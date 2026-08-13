"""Post-hoc checkpoint selection for RL-continue-v2 (phase 2, configs/
bird_adapt_rl_v2_phase2.yaml).

runs/bird_adapt_rl's health_log showed train_reward_mean oscillating step
to step rather than climbing monotonically, so the very last checkpoint
isn't guaranteed to be the best one produced -- same reasoning already
applied to picking between SFT checkpoint-500/checkpoint-750 earlier in
this project. This scores every saved checkpoint from phase 2 against
data/bird_adapt/bird_pool_heldout.jsonl ONLY (31 examples, same 2 schemas
as training, never used for gradient updates) and copies the winner's
adapter weights into runs/bird_adapt_rl_v2/adapter, where the existing eval
tooling (configs/bird_adapt_rl_v2_eval*.yaml, scripts/run_bird_adapt_eval.sh)
already expects to find it.

Deliberately does NOT look at bird_crossdb_eval here. Selecting a
checkpoint by its score on the actual transfer-test set, then reporting
that same set's score as "the result," is test-set leakage -- it would
silently inflate the reported crossdb number through implicit tuning.
crossdb_eval is scored exactly once, after this script has already fixed
which checkpoint is "the" RL-continue-v2 adapter, same separation of
concerns eval_sql.py/generate_sql.py already keep between train and dev data.

Each checkpoint is scored with a full, independent model reload (base +
that checkpoint's adapter), matching every other eval script in this
project -- PEFT's in-place adapter patching makes reusing one loaded base
model across multiple adapters within a process an unverified pattern this
project has consistently avoided; a few extra reloads over ~6 checkpoints
on 31 examples each is a small, safe cost for that consistency.

Usage:
  python scripts/select_best_bird_adapt_rl_checkpoint.py \
      --run-dir runs/bird_adapt_rl_v2 \
      --pool-heldout data/bird_adapt/bird_pool_heldout.jsonl \
      --db-dir data/bird-dev/dev_databases
"""

import argparse
import glob
import json
import os
import shutil

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
from peft import PeftModel

from eval_sql import execute_query, get_connection, rows_match
from prompt_template import build_messages
from sql_extract import extract_sql

ADAPTER_FILES = ["adapter_config.json", "adapter_model.safetensors"]


def load_heldout(path):
    rows = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def load_model_with_adapter(model_path, adapter_path, dtype):
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


def generate_greedy(model, tokenizer, device, rows, batch_size, max_new_tokens):
    preds = []
    for start in range(0, len(rows), batch_size):
        batch = rows[start:start + batch_size]
        prompts = [
            tokenizer.apply_chat_template(build_messages(r["question"], r["schema"]),
                                           tokenize=False, add_generation_prompt=True)
            for r in batch
        ]
        inputs = tokenizer(prompts, return_tensors="pt", padding=True, truncation=True, max_length=4096).to(device)
        with torch.no_grad():
            output_ids = model.generate(
                **inputs, max_new_tokens=max_new_tokens, do_sample=False,
                pad_token_id=tokenizer.pad_token_id,
            )
        gen_only = output_ids[:, inputs["input_ids"].shape[1]:]
        raw = tokenizer.batch_decode(gen_only, skip_special_tokens=True)
        preds.extend(extract_sql(r) for r in raw)
    return preds


def score_pure_accuracy(preds, rows, db_dir, timeout_sec=10):
    conn_cache = {}
    correct = 0
    for pred_sql, row in zip(preds, rows):
        conn = get_connection(db_dir, row["db_id"], conn_cache)
        order_matters = "order by" in row["gold_sql"].lower()
        gold_rows, gold_err = execute_query(conn, row["gold_sql"], timeout_sec)
        if gold_err is not None:
            continue  # data problem, not scored either way -- same convention as eval_sql.py
        pred_rows, pred_err = execute_query(conn, pred_sql, timeout_sec)
        if pred_err is None and rows_match(gold_rows, pred_rows, order_matters):
            correct += 1
    return correct


def find_checkpoints(run_dir):
    ckpts = sorted(
        glob.glob(os.path.join(run_dir, "checkpoint-*")),
        key=lambda p: int(p.rsplit("-", 1)[-1]),
    )
    return ckpts


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="/home/levi/Documents/llm/Qwen2.5-Coder-3B-Instruct")
    parser.add_argument("--run-dir", default="runs/bird_adapt_rl_v2")
    parser.add_argument("--pool-heldout", default="data/bird_adapt/bird_pool_heldout.jsonl")
    parser.add_argument("--db-dir", default="data/bird-dev/dev_databases")
    parser.add_argument("--batch-size", type=int, default=4)
    parser.add_argument("--max-new-tokens", type=int, default=256)
    args = parser.parse_args()

    if not torch.cuda.is_available():
        print("[select_checkpoint] WARNING: CUDA not available, this will be very slow.")

    heldout = load_heldout(args.pool_heldout)
    print(f"[select_checkpoint] Loaded {len(heldout)} pool-heldout examples")

    checkpoints = find_checkpoints(args.run_dir)
    final_adapter = os.path.join(args.run_dir, "adapter")
    candidates = checkpoints + ([final_adapter] if os.path.exists(final_adapter) else [])
    if not candidates:
        raise RuntimeError(f"No checkpoints or final adapter found under {args.run_dir}")
    print(f"[select_checkpoint] Scoring {len(candidates)} candidates: {candidates}")

    results = []
    for adapter_path in candidates:
        print(f"\n[select_checkpoint] === {adapter_path} ===")
        model, tokenizer = load_model_with_adapter(args.model, adapter_path, torch.bfloat16)
        device = next(model.parameters()).device
        preds = generate_greedy(model, tokenizer, device, heldout, args.batch_size, args.max_new_tokens)
        correct = score_pure_accuracy(preds, heldout, args.db_dir)
        accuracy = correct / len(heldout) if heldout else 0.0
        print(f"[select_checkpoint] {adapter_path}: {correct}/{len(heldout)} = {accuracy:.4f}")
        results.append({"path": adapter_path, "correct": correct, "total": len(heldout), "accuracy": accuracy})
        del model
        torch.cuda.empty_cache()

    results.sort(key=lambda r: r["accuracy"], reverse=True)
    print("\n[select_checkpoint] === Ranked (by pool-heldout accuracy only) ===")
    for r in results:
        print(f"  {r['accuracy']:.4f}  ({r['correct']}/{r['total']})  {r['path']}")

    winner = results[0]
    print(f"\n[select_checkpoint] WINNER: {winner['path']} ({winner['accuracy']:.4f})")

    out_adapter_dir = final_adapter
    if winner["path"] != final_adapter:
        os.makedirs(out_adapter_dir, exist_ok=True)
        for fname in ADAPTER_FILES:
            src = os.path.join(winner["path"], fname)
            if not os.path.exists(src):
                raise FileNotFoundError(f"Winning checkpoint is missing {fname}: {src}")
            shutil.copy2(src, os.path.join(out_adapter_dir, fname))
        print(f"[select_checkpoint] Copied {winner['path']}'s adapter files into {out_adapter_dir} "
              "(overwriting the final-step adapter) so existing eval configs point at the winner.")
    else:
        print(f"[select_checkpoint] Winner was already the final adapter at {out_adapter_dir} -- nothing to copy.")

    report_path = os.path.join(args.run_dir, "checkpoint_selection_report.json")
    with open(report_path, "w") as f:
        json.dump({"candidates": results, "winner": winner}, f, indent=2)
    print(f"[select_checkpoint] Wrote selection report to {report_path}")


if __name__ == "__main__":
    main()
