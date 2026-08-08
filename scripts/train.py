"""
Config-driven LoRA SFT training for Windows/CUDA, replacing mlx_lm_lora.train
(which only runs on Apple Silicon -- MLX has no Windows build, it isn't a
missing pip package, it's a hard platform restriction). This is the
Windows-native equivalent: transformers + peft + trl + bitsandbytes, same
role mlx_lm_lora.train played on the Mac, same "one YAML config per run" UX.

Usage (matches the mlx_lm_lora.train --config pattern you asked for):
    python scripts/train.py --config configs/sft_diverse_rank16.yaml
    python scripts/train.py --config configs/sft_narrow_rank16.yaml

Data format is UNCHANGED from the Mac version: train.jsonl/valid.jsonl with
one {"messages": [{"role": ..., "content": ...}, ...]} object per line,
exactly what prepare_spider_data.py already produces under data/sft_diverse/
and data/sft_narrow/. trl's SFTTrainer accepts a "messages" column directly
and applies the tokenizer's chat template itself, so nothing in scripts/
sql_prompt.py or scripts/prepare_spider_data.py needs to change.

Version-sensitivity warning: trl's exact SFTConfig field names for
"only compute loss on the assistant turn" have changed across releases
(seen as completion_only_loss, assistant_only_loss, and DataCollator-based
approaches in different versions). This script tries completion_only_loss
first, matching current (2026) trl, and falls back to assistant_only_loss on
TypeError. If BOTH fail on your installed version, run:
    python -c "from trl import SFTConfig; import inspect; print(inspect.signature(SFTConfig.__init__))"
and tell me what field name it actually wants -- one-line fix.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import yaml


def build_and_train(config: dict):
    import torch
    from datasets import load_dataset
    from peft import LoraConfig, get_peft_model
    from transformers import (
        AutoModelForCausalLM,
        AutoTokenizer,
        BitsAndBytesConfig,
        EarlyStoppingCallback,
    )
    from trl import SFTConfig, SFTTrainer

    model_cfg = config["model"]
    data_cfg = config["data"]
    lora_cfg = config["lora"]
    train_cfg = dict(config["training"])  # copy, we pop keys below
    es_cfg = train_cfg.pop("early_stopping", {"enabled": False})

    model_name = model_cfg["name_or_path"]
    print(f"Loading base model: {model_name}")

    quantization_config = None
    if model_cfg.get("load_in_4bit", False):
        quantization_config = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype=torch.bfloat16,
            bnb_4bit_use_double_quant=True,
        )

    tokenizer = AutoTokenizer.from_pretrained(model_name)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    model = AutoModelForCausalLM.from_pretrained(
        model_name,
        quantization_config=quantization_config,
        device_map="auto",
        torch_dtype=torch.bfloat16,
    )

    peft_config = LoraConfig(
        r=lora_cfg["rank"],
        lora_alpha=lora_cfg.get("alpha", lora_cfg["rank"] * 2),
        lora_dropout=lora_cfg.get("dropout", 0.05),
        target_modules=lora_cfg.get("target_modules", ["q_proj", "k_proj", "v_proj", "o_proj"]),
        bias="none",
        task_type="CAUSAL_LM",
    )

    train_ds = load_dataset("json", data_files=data_cfg["train_file"], split="train")
    valid_ds = None
    if data_cfg.get("valid_file"):
        valid_ds = load_dataset("json", data_files=data_cfg["valid_file"], split="train")
    print(f"Loaded {len(train_ds)} train examples" + (f", {len(valid_ds)} valid examples" if valid_ds else ""))

    output_dir = train_cfg.pop("output_dir")
    max_seq_length = data_cfg.get("max_seq_length", 1536)

    sft_config_kwargs = dict(
        output_dir=output_dir,
        max_length=max_seq_length,
        packing=False,  # keep one example per sequence, so completion-only loss masking stays correct
        **train_cfg,
    )

    if es_cfg.get("enabled", False):
        sft_config_kwargs.setdefault("load_best_model_at_end", True)
        sft_config_kwargs.setdefault("metric_for_best_model", es_cfg.get("metric", "eval_loss"))
        sft_config_kwargs.setdefault("greater_is_better", False)

    try:
        sft_config = SFTConfig(completion_only_loss=True, **sft_config_kwargs)
    except TypeError:
        sft_config = SFTConfig(assistant_only_loss=True, **sft_config_kwargs)

    callbacks = []
    if es_cfg.get("enabled", False):
        callbacks.append(EarlyStoppingCallback(early_stopping_patience=es_cfg.get("patience", 3)))
        print(f"Early stopping enabled: patience={es_cfg.get('patience', 3)} on {es_cfg.get('metric', 'eval_loss')}")

    peft_model = get_peft_model(model, peft_config)
    peft_model.print_trainable_parameters()

    trainer = SFTTrainer(
        model=peft_model,
        args=sft_config,
        train_dataset=train_ds,
        eval_dataset=valid_ds,
        processing_class=tokenizer,
        callbacks=callbacks,
    )

    trainer.train()

    print(f"Saving final adapter to {output_dir}")
    trainer.save_model(output_dir)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True, help="Path to a YAML config, e.g. configs/sft_diverse_rank16.yaml")
    args = ap.parse_args()

    with open(args.config, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    build_and_train(config)


if __name__ == "__main__":
    main()
