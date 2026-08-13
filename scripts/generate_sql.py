"""Generate SQL predictions for the zero-shot baseline arm.

Loads Qwen2.5-Coder-3B-Instruct (untouched, no fine-tuning) and runs it
over a Spider/BIRD dev split, one prompt per example, greedy by default.
Writes two files under <run_dir>/:

  {dataset}_{split}_preds.sql   -- one predicted SQL query per line, in the
                                    same order as the input examples, so it
                                    line-aligns with the matching *_gold.sql
                                    file for eval_sql.py.
  {dataset}_{split}_raw.jsonl   -- one record per example with the full
                                    prompt and raw model output, kept for
                                    debugging and for the error-taxonomy
                                    analysis described in plan.md.

Usage:
  python scripts/generate_sql.py --config configs/baseline.yaml --dataset spider --split dev
"""

import argparse
import json
import os
import sys

import torch
from tqdm import tqdm
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig

from data_utils import load_config, load_examples, resolve_dataset_paths
from peft import PeftModel
from prompt_template import build_messages
from schema_utils import get_schema_str, load_tables
from sql_extract import extract_sql


def load_model_and_tokenizer(model_cfg: dict):
    model_path = model_cfg["name_or_path"]
    dtype = getattr(torch, model_cfg.get("dtype", "bfloat16"))
    device = model_cfg.get("device", "cuda")
    if device == "cuda" and not torch.cuda.is_available():
        print("[generate_sql] CUDA not available, falling back to CPU (this will be slow).")
        device = "cpu"

    tokenizer = AutoTokenizer.from_pretrained(model_path)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    tokenizer.padding_side = "left"  # required for correct batched causal generation

    attn_implementation = model_cfg.get("attn_implementation", "sdpa")
    if model_cfg.get("load_in_4bit", False):
        bnb_config = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype=dtype,
        )
        model = AutoModelForCausalLM.from_pretrained(
            model_path,
            torch_dtype=dtype,
            device_map="auto",
            quantization_config=bnb_config,
            attn_implementation=attn_implementation,
        )
    else:
        model = AutoModelForCausalLM.from_pretrained(
            model_path, torch_dtype=dtype, attn_implementation=attn_implementation
        )
        model.to(device)

    adapter_path = model_cfg.get("adapter")
    if adapter_path:
        # SFT/RL arms: load the LoRA adapter on top of the frozen base weights
        # loaded above. The adapter must have been trained against a base
        # model loaded the same way (same load_in_4bit setting) it is
        # loaded here -- train_sft.py always trains in 4-bit NF4, so
        # load_in_4bit should be true in the eval config too (see
        # configs/sft_eval.yaml).
        print(f"[generate_sql] Loading LoRA adapter from {adapter_path} ...")
        model = PeftModel.from_pretrained(model, adapter_path)

    model.eval()
    return model, tokenizer, device


def generate_batch(model, tokenizer, device, batch_messages, gen_cfg):
    prompts = [
        tokenizer.apply_chat_template(m, tokenize=False, add_generation_prompt=True)
        for m in batch_messages
    ]
    inputs = tokenizer(
        prompts, return_tensors="pt", padding=True, truncation=True, max_length=4096
    ).to(device)

    gen_kwargs = dict(
        max_new_tokens=gen_cfg["max_new_tokens"],
        do_sample=gen_cfg.get("do_sample", False),
        num_beams=gen_cfg.get("num_beams", 1),
        pad_token_id=tokenizer.pad_token_id,
    )
    if gen_kwargs["do_sample"]:
        gen_kwargs["temperature"] = gen_cfg.get("temperature", 1.0)

    with torch.no_grad():
        output_ids = model.generate(**inputs, **gen_kwargs)

    gen_only = output_ids[:, inputs["input_ids"].shape[1] :]
    return tokenizer.batch_decode(gen_only, skip_special_tokens=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="configs/baseline.yaml")
    parser.add_argument("--dataset", choices=["spider", "bird"], default="spider")
    parser.add_argument("--split", default="dev", help="Label only, used for output filenames.")
    parser.add_argument("--limit", type=int, default=None, help="Only run the first N examples (debug).")
    parser.add_argument("--output-dir", default=None, help="Override config's output.run_dir.")
    args = parser.parse_args()

    config = load_config(args.config)
    paths = resolve_dataset_paths(config, args.dataset)
    if paths is None:
        print(f"[generate_sql] No usable paths for dataset '{args.dataset}'. Exiting.")
        sys.exit(1)

    run_dir = args.output_dir or config["output"]["run_dir"]
    os.makedirs(run_dir, exist_ok=True)

    tables_by_db = load_tables(paths["tables"])
    examples = load_examples(paths["dev"])
    if args.limit:
        examples = examples[: args.limit]

    print(f"[generate_sql] Loaded {len(examples)} examples from {paths['dev']}")
    print(f"[generate_sql] Loading model from {config['model']['name_or_path']} ...")
    model, tokenizer, device = load_model_and_tokenizer(config["model"])

    batch_size = config["generation"].get("batch_size", 4)
    preds_path = os.path.join(run_dir, f"{args.dataset}_{args.split}_preds.sql")
    raw_path = os.path.join(run_dir, f"{args.dataset}_{args.split}_raw.jsonl")

    with open(preds_path, "w") as preds_f, open(raw_path, "w") as raw_f:
        for start in tqdm(range(0, len(examples), batch_size), desc="Generating"):
            batch = examples[start : start + batch_size]
            batch_messages = []
            for ex in batch:
                schema_str = get_schema_str(ex["db_id"], tables_by_db)
                batch_messages.append(build_messages(ex["question"], schema_str))

            raw_outputs = generate_batch(model, tokenizer, device, batch_messages, config["generation"])

            for ex, raw_output in zip(batch, raw_outputs):
                pred_sql = extract_sql(raw_output)
                preds_f.write(pred_sql + "\n")
                raw_f.write(
                    json.dumps(
                        {
                            "db_id": ex["db_id"],
                            "question": ex["question"],
                            "raw_output": raw_output,
                            "pred_sql": pred_sql,
                        }
                    )
                    + "\n"
                )

    print(f"[generate_sql] Wrote predictions to {preds_path}")
    print(f"[generate_sql] Wrote raw generations to {raw_path}")


if __name__ == "__main__":
    main()
