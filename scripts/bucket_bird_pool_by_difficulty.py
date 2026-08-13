"""Curriculum step 1 for RL-continue-v2: bucket the fixed 122-example BIRD
training pool by how "reachable" each example already is for the shared
starting checkpoint, BEFORE any BIRD-slice training. This is what the
first RL-continue attempt was missing -- health_log.jsonl showed
frac_reward_zero_std/low reward the entire run because most groups were
either all-correct or (mostly) all-wrong for the starting policy, leaving
GRPO's (r - mean) / std advantage undefined or unhelpful most of the time.

Adds NO new examples -- same 122 rows as bird_train_pool.jsonl throughout,
this only classifies and reorders them. For each example: generate
num_generations samples with the SAME sampling settings configs/bird_adapt_rl.yaml
used (temperature 1.3), score each with the real execution reward, and
bucket by how many of the samples were exactly correct:
  hard   : 0 correct out of N   -- no learnable signal, a zero-variance group
  mixed  : 1..N-1 correct       -- the useful bucket, has within-group reward
                                    variance for GRPO to compute a real advantage from
  easy   : N correct            -- already solved, little left to learn

Writes bird_train_pool_reachable.jsonl (the "mixed" bucket only) for
curriculum phase 1 (configs/bird_adapt_rl_v2_phase1.yaml), and
bucket_report.json with the full breakdown for inspection.

Usage:
  python scripts/bucket_bird_pool_by_difficulty.py \
      --adapter runs/sft_qwen2.5coder3b/adapter \
      --pool data/bird_adapt/bird_train_pool.jsonl \
      --db-dir data/bird-dev/dev_databases \
      --out-dir data/bird_adapt
"""

import argparse
import json
import os

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
from peft import PeftModel

from prompt_template import build_messages
from rl_reward import make_execution_reward
from sql_extract import extract_sql


def load_pool(path):
    rows = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def load_model(model_path, adapter_path, dtype):
    tokenizer = AutoTokenizer.from_pretrained(model_path)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    tokenizer.padding_side = "left"

    bnb_config = BitsAndBytesConfig(
        load_in_4bit=True, bnb_4bit_quant_type="nf4",
        bnb_4bit_compute_dtype=dtype, bnb_4bit_use_double_quant=True,
    )
    model = AutoModelForCausalLM.from_pretrained(
        model_path, torch_dtype=dtype, device_map="auto",
        quantization_config=bnb_config, attn_implementation="sdpa",
    )
    model = PeftModel.from_pretrained(model, adapter_path)
    model.eval()
    return model, tokenizer


def generate_n_samples(model, tokenizer, device, row, n, temperature, max_new_tokens):
    messages = build_messages(row["question"], row["schema"])
    prompt = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    inputs = tokenizer([prompt] * n, return_tensors="pt", padding=True, truncation=True, max_length=4096).to(device)
    with torch.no_grad():
        output_ids = model.generate(
            **inputs, max_new_tokens=max_new_tokens, do_sample=True,
            temperature=temperature, pad_token_id=tokenizer.pad_token_id,
        )
    gen_only = output_ids[:, inputs["input_ids"].shape[1]:]
    raw = tokenizer.batch_decode(gen_only, skip_special_tokens=True)
    return [extract_sql(r) for r in raw]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="/home/levi/Documents/llm/Qwen2.5-Coder-3B-Instruct")
    parser.add_argument("--adapter", default="runs/sft_qwen2.5coder3b/adapter")
    parser.add_argument("--pool", default="data/bird_adapt/bird_train_pool.jsonl")
    parser.add_argument("--db-dir", default="data/bird-dev/dev_databases")
    parser.add_argument("--out-dir", default="data/bird_adapt")
    parser.add_argument("--num-samples", type=int, default=4, help="Should match grpo.num_generations")
    parser.add_argument("--temperature", type=float, default=1.3, help="Should match grpo.temperature")
    parser.add_argument("--max-new-tokens", type=int, default=256)
    args = parser.parse_args()

    if not torch.cuda.is_available():
        print("[bucket] WARNING: CUDA not available, this will be very slow.")

    pool = load_pool(args.pool)
    print(f"[bucket] Loaded {len(pool)} pool examples from {args.pool}")

    print(f"[bucket] Loading {args.model} + adapter {args.adapter} ...")
    model, tokenizer = load_model(args.model, args.adapter, torch.bfloat16)
    device = next(model.parameters()).device

    reward_fn = make_execution_reward(args.db_dir, partial_credit=0.0)  # pure match, for bucketing only

    buckets = {"hard": [], "mixed": [], "easy": []}
    per_example_report = []
    for i, row in enumerate(pool):
        preds = generate_n_samples(model, tokenizer, device, row, args.num_samples, args.temperature, args.max_new_tokens)
        rewards = reward_fn(
            [None] * len(preds), preds,
            [row["db_id"]] * len(preds), [row["gold_sql"]] * len(preds),
        )
        n_correct = sum(1 for r in rewards if r == 1.0)
        if n_correct == 0:
            bucket = "hard"
        elif n_correct == args.num_samples:
            bucket = "easy"
        else:
            bucket = "mixed"
        buckets[bucket].append(row)
        per_example_report.append({
            "index": i, "db_id": row["db_id"], "question": row["question"],
            "n_correct": n_correct, "n_samples": args.num_samples, "bucket": bucket,
        })
        if (i + 1) % 20 == 0 or i == len(pool) - 1:
            print(f"[bucket] {i + 1}/{len(pool)} scored -- "
                  f"hard={len(buckets['hard'])} mixed={len(buckets['mixed'])} easy={len(buckets['easy'])}")

    print(f"\n[bucket] Final: hard={len(buckets['hard'])} mixed={len(buckets['mixed'])} easy={len(buckets['easy'])} "
          f"out of {len(pool)} total")

    if not buckets["mixed"]:
        print("[bucket] WARNING: the 'mixed' bucket is empty -- every example is either always-right or "
              "always-wrong for the starting checkpoint at this temperature/sample count. Curriculum phase 1 "
              "would have nothing to train on; consider raising --num-samples or --temperature and re-running.")

    out_dir = args.out_dir
    reachable_path = os.path.join(out_dir, "bird_train_pool_reachable.jsonl")
    with open(reachable_path, "w") as f:
        for row in buckets["mixed"]:
            f.write(json.dumps({"db_id": row["db_id"], "question": row["question"],
                                 "schema": row["schema"], "gold_sql": row["gold_sql"]}) + "\n")
    print(f"[bucket] Wrote {len(buckets['mixed'])} 'mixed' rows to {reachable_path} (curriculum phase 1 data)")

    report_path = os.path.join(out_dir, "bucket_report.json")
    with open(report_path, "w") as f:
        json.dump({
            "num_samples": args.num_samples, "temperature": args.temperature,
            "counts": {k: len(v) for k, v in buckets.items()},
            "examples": per_example_report,
        }, f, indent=2)
    print(f"[bucket] Wrote full report to {report_path}")


if __name__ == "__main__":
    main()
