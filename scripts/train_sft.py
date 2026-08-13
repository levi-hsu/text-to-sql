"""QLoRA SFT training for the SFT arm (see plan.md, "Arms" / "Model").

Base weights are quantized to 4-bit NF4 at load time via bitsandbytes and
frozen; LoRA adapters are trained on top in bf16. Only the adapter is
updated and saved -- the base model on disk is never modified, which is
what lets the RL arm later reload the same base and initialize from this
adapter (plan.md: "RL arm: initialized from the SFT checkpoint").

Loss is computed only on the assistant turn (the gold SQL). The prompt
tokens (system + user turns, i.e. schema + question) are masked to -100 so
the model is trained to produce SQL given the prompt, not to reproduce the
prompt itself -- standard instruction-tuning masking, not implemented via
TRL's SFTTrainer here so the masking boundary is explicit and easy to
verify against prompt_template.py.

Input: the JSONL produced by build_sft_data.py (data.sft_data_out in the
config), each line {db_id, question, schema, gold_sql}.

Output: runs/sft_qwen2.5coder3b/adapter/ -- a PEFT adapter directory
(adapter_config.json + adapter weights) plus the tokenizer, loadable by
generate_sql.py via the model.adapter config field (see configs/sft_eval.yaml).

model.resume_adapter (optional): if set, continues training that existing
PEFT adapter (loaded with is_trainable=True, same pattern train_rl.py
already uses for the RL arm) instead of initializing a fresh LoRA adapter
from config["lora"] -- config["lora"] is not read at all in that case, so a
resume config doesn't need a lora: block. Added for the small-BIRD-slice
SFT-continuation arm (configs/bird_adapt_sft.yaml), which continues the
Spider SFT arm's own final adapter rather than training a new one from
scratch on a tiny slice. Omit resume_adapter (as configs/sft.yaml does) to
get the original fresh-adapter behavior, unchanged.

Usage:
  python scripts/train_sft.py --config configs/sft.yaml
"""

import argparse
import json
import os

# Must be set before torch initializes its CUDA allocator (i.e. before the
# `import torch` below does anything CUDA-related). Reduces OOM risk from
# allocator fragmentation on an 8GB card running near its ceiling -- see
# the OOM warning encountered during the first training run at
# max_seq_length=1024, batch_size=1: free memory bottomed out at ~200MB
# mid-run. Does not increase the memory budget itself, only how
# efficiently the freed cache is reused.
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")

import torch
from torch.utils.data import Dataset
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    BitsAndBytesConfig,
    Trainer,
    TrainingArguments,
)
from peft import LoraConfig, PeftModel, get_peft_model, prepare_model_for_kbit_training

from data_utils import load_config
from prompt_template import build_messages

IGNORE_INDEX = -100


class SFTDataset(Dataset):
    """Tokenizes each (schema, question, gold_sql) row into a masked example.

    Each item is built independently to its own length; padding to a
    common length within a batch is handled by the collate function below,
    not here, since batches can mix short and long examples.
    """

    def __init__(self, jsonl_path: str, tokenizer, max_seq_length: int):
        self.tokenizer = tokenizer
        self.max_seq_length = max_seq_length
        self.rows = []
        with open(jsonl_path, "r") as f:
            for line in f:
                line = line.strip()
                if line:
                    self.rows.append(json.loads(line))

    def __len__(self):
        return len(self.rows)

    def __getitem__(self, idx):
        row = self.rows[idx]
        prompt_messages = build_messages(row["question"], row["schema"])
        full_messages = prompt_messages + [{"role": "assistant", "content": row["gold_sql"]}]

        # Render to text first, then tokenize explicitly with the plain
        # __call__ API. apply_chat_template(tokenize=True) returns a plain
        # list[int] on some transformers versions but a tokenizers.Encoding
        # (or other wrapper) on others -- going through text avoids that
        # version-dependent branch entirely.
        prompt_text = self.tokenizer.apply_chat_template(
            prompt_messages, tokenize=False, add_generation_prompt=True
        )
        full_text = self.tokenizer.apply_chat_template(
            full_messages, tokenize=False, add_generation_prompt=False
        )
        prompt_ids = self.tokenizer(prompt_text, add_special_tokens=False)["input_ids"]
        full_ids = self.tokenizer(full_text, add_special_tokens=False)["input_ids"]

        # full_ids should start with prompt_ids as a prefix (chat template is
        # deterministic given the same leading turns); guard against the rare
        # case where it doesn't, by masking to len(prompt_ids) regardless.
        prompt_len = min(len(prompt_ids), len(full_ids))

        input_ids = full_ids[: self.max_seq_length]
        labels = list(input_ids)
        for i in range(min(prompt_len, len(labels))):
            labels[i] = IGNORE_INDEX

        return {
            "input_ids": input_ids,
            "labels": labels,
            "attention_mask": [1] * len(input_ids),
        }


def make_collate_fn(pad_token_id: int):
    def collate(batch):
        max_len = max(len(ex["input_ids"]) for ex in batch)
        input_ids, labels, attention_mask = [], [], []
        for ex in batch:
            pad_len = max_len - len(ex["input_ids"])
            input_ids.append(ex["input_ids"] + [pad_token_id] * pad_len)
            labels.append(ex["labels"] + [IGNORE_INDEX] * pad_len)
            attention_mask.append(ex["attention_mask"] + [0] * pad_len)
        return {
            "input_ids": torch.tensor(input_ids, dtype=torch.long),
            "labels": torch.tensor(labels, dtype=torch.long),
            "attention_mask": torch.tensor(attention_mask, dtype=torch.long),
        }

    return collate


def load_base_model_for_training(model_cfg: dict):
    model_path = model_cfg["name_or_path"]
    dtype = getattr(torch, model_cfg.get("dtype", "bfloat16"))

    bnb_config = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_compute_dtype=dtype,
        bnb_4bit_use_double_quant=True,
    )
    model = AutoModelForCausalLM.from_pretrained(
        model_path,
        torch_dtype=dtype,
        device_map="auto",
        quantization_config=bnb_config,
        # sdpa's fused attention kernel avoids materializing the full
        # seq_len x seq_len attention matrix that eager attention keeps
        # around for backward -- on an 8GB card this is the difference
        # between OOM and not at sequence lengths near max_seq_length.
        attn_implementation=model_cfg.get("attn_implementation", "sdpa"),
    )
    model = prepare_model_for_kbit_training(model, use_gradient_checkpointing=True)
    return model


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="configs/sft.yaml")
    args = parser.parse_args()

    config = load_config(args.config)
    data_cfg = config["data"]
    train_cfg = config["training"]
    resume_adapter = config["model"].get("resume_adapter")

    if not torch.cuda.is_available():
        print(
            "[train_sft] WARNING: CUDA not available. QLoRA (bitsandbytes 4-bit) "
            "requires a CUDA GPU; this will fail or be unusably slow on CPU."
        )

    print(f"[train_sft] Loading tokenizer from {config['model']['name_or_path']} ...")
    tokenizer = AutoTokenizer.from_pretrained(config["model"]["name_or_path"])
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    tokenizer.padding_side = "right"  # training, not generation -- labels align left-to-right

    print(f"[train_sft] Loading base model in 4-bit NF4 ...")
    model = load_base_model_for_training(config["model"])

    if resume_adapter:
        print(f"[train_sft] Continuing training from existing adapter at {resume_adapter} "
              "(model.resume_adapter set -- config['lora'] is ignored) ...")
        model = PeftModel.from_pretrained(model, resume_adapter, is_trainable=True)
    else:
        lora_cfg = config["lora"]
        peft_config = LoraConfig(
            r=lora_cfg["r"],
            lora_alpha=lora_cfg["alpha"],
            lora_dropout=lora_cfg["dropout"],
            target_modules=lora_cfg["target_modules"],
            bias="none",
            task_type="CAUSAL_LM",
        )
        model = get_peft_model(model, peft_config)
    model.print_trainable_parameters()

    train_dataset = SFTDataset(
        data_cfg["sft_data_out"], tokenizer, max_seq_length=data_cfg["max_seq_length"]
    )
    print(f"[train_sft] Loaded {len(train_dataset)} training examples from {data_cfg['sft_data_out']}")
    if len(train_dataset) == 0:
        raise RuntimeError(
            f"No training examples found at {data_cfg['sft_data_out']}. "
            "Run scripts/build_sft_data.py first."
        )

    output_dir = train_cfg["output_dir"]
    os.makedirs(output_dir, exist_ok=True)

    training_args = TrainingArguments(
        output_dir=output_dir,
        num_train_epochs=train_cfg["num_train_epochs"],
        per_device_train_batch_size=train_cfg["per_device_train_batch_size"],
        gradient_accumulation_steps=train_cfg["gradient_accumulation_steps"],
        learning_rate=train_cfg["learning_rate"],
        lr_scheduler_type=train_cfg.get("lr_scheduler_type", "cosine"),
        warmup_ratio=train_cfg.get("warmup_ratio", 0.03),
        logging_steps=train_cfg.get("logging_steps", 10),
        save_strategy=train_cfg.get("save_strategy", "epoch"),
        save_total_limit=train_cfg.get("save_total_limit", 2),
        bf16=train_cfg.get("bf16", True),
        gradient_checkpointing=train_cfg.get("gradient_checkpointing", True),
        gradient_checkpointing_kwargs={"use_reentrant": False},
        optim=train_cfg.get("optim", "paged_adamw_8bit"),
        report_to=train_cfg.get("report_to", "none"),
        dataloader_num_workers=train_cfg.get("dataloader_num_workers", 2),
        remove_unused_columns=False,
    )

    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=train_dataset,
        data_collator=make_collate_fn(tokenizer.pad_token_id),
    )

    trainer.train()

    adapter_dir = os.path.join(output_dir, "adapter")
    model.save_pretrained(adapter_dir)
    tokenizer.save_pretrained(adapter_dir)
    print(f"[train_sft] Saved LoRA adapter + tokenizer to {adapter_dir}")


if __name__ == "__main__":
    main()
