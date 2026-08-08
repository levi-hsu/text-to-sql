"""
Generation + EX scoring over an eval JSONL built by prepare_spider_data.py
(data/eval/dev.jsonl or data/eval/narrow_heldout.jsonl).

Windows/CUDA version: uses transformers + peft instead of mlx_lm (mlx_lm
only runs on Apple Silicon). Everything in sql_prompt.py and eval_sql.py is
plain Python and already tested without any ML framework; this script is
the thin transformers-specific layer on top of that, mirroring the role the
mlx_lm version played on the Mac.

Usage, base model, no adapter (the PLAN.md "baseline evaluation" step):
    python scripts/run_baseline_eval.py ^
        --model Qwen/Qwen2.5-Coder-7B-Instruct ^
        --eval-file data/eval/dev.jsonl ^
        --spider-dir spider_data ^
        --out-dir runs/baseline_dev ^
        --limit 50

Usage, with a trained LoRA adapter (after Stage 1 SFT):
    python scripts/run_baseline_eval.py ^
        --model Qwen/Qwen2.5-Coder-7B-Instruct ^
        --adapter-path runs/sft_diverse/adapters ^
        --eval-file data/eval/dev.jsonl ^
        --spider-dir spider_data ^
        --out-dir runs/sft_diverse_dev

(^ is the PowerShell/cmd line-continuation character, equivalent to \\ on
macOS/Linux -- if you're pasting into PowerShell use ^ or put it all on one
line; backtick ` is the PowerShell-native continuation char if you prefer
that instead.)

--limit caps the number of eval examples for a quick smoke test; omit it (or
set to 0) to run the full eval file.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from sql_prompt import build_chat_messages
from eval_sql import score_file


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True, help="HF Hub id or local path to the base model")
    ap.add_argument("--adapter-path", default=None, help="Path to a trained LoRA adapter directory, if evaluating a fine-tuned checkpoint")
    ap.add_argument("--eval-file", required=True, help="e.g. data/eval/dev.jsonl or data/eval/narrow_heldout.jsonl")
    ap.add_argument("--spider-dir", default="spider_data")
    ap.add_argument("--out-dir", required=True, help="Where to write predictions.jsonl and results.json")
    ap.add_argument("--max-new-tokens", type=int, default=256)
    ap.add_argument("--load-in-4bit", action="store_true", default=True, help="4-bit load via bitsandbytes (default on, matches an 8-12GB VRAM budget)")
    ap.add_argument("--no-4bit", dest="load_in_4bit", action="store_false")
    ap.add_argument("--limit", type=int, default=0, help="Cap number of eval examples (0 = all)")
    args = ap.parse_args()

    # Imported here, not at module top, so --help and the rest of this file
    # stay importable/testable even where torch/transformers aren't installed.
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig

    quantization_config = None
    if args.load_in_4bit:
        quantization_config = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype=torch.bfloat16,
            bnb_4bit_use_double_quant=True,
        )

    print(f"Loading model {args.model}" + (f" with adapter {args.adapter_path}" if args.adapter_path else " (no adapter, base model)"))
    tokenizer = AutoTokenizer.from_pretrained(args.model)
    model = AutoModelForCausalLM.from_pretrained(
        args.model,
        quantization_config=quantization_config,
        device_map="auto",
        torch_dtype=torch.bfloat16,
    )
    if args.adapter_path:
        from peft import PeftModel
        model = PeftModel.from_pretrained(model, args.adapter_path)
    model.eval()

    eval_records = [json.loads(l) for l in open(args.eval_file, encoding="utf-8")]
    if args.limit:
        eval_records = eval_records[: args.limit]
    print(f"Generating for {len(eval_records)} examples from {args.eval_file}")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    pred_path = out_dir / "predictions.jsonl"

    t0 = time.time()
    with open(pred_path, "w", encoding="utf-8") as f:
        for i, rec in enumerate(eval_records):
            messages = build_chat_messages(rec["schema_ddl"], rec["question"])
            input_ids = tokenizer.apply_chat_template(
                messages, add_generation_prompt=True, return_tensors="pt"
            ).to(model.device)
            with torch.no_grad():
                output_ids = model.generate(
                    input_ids,
                    max_new_tokens=args.max_new_tokens,
                    do_sample=False,  # greedy, for reproducible eval numbers
                    pad_token_id=tokenizer.pad_token_id or tokenizer.eos_token_id,
                )
            new_tokens = output_ids[0][input_ids.shape[1]:]
            raw = tokenizer.decode(new_tokens, skip_special_tokens=True)
            f.write(json.dumps({"prediction": raw}) + "\n")
            f.flush()
            if (i + 1) % 25 == 0:
                elapsed = time.time() - t0
                print(f"  {i + 1}/{len(eval_records)} generated ({elapsed:.1f}s elapsed, {elapsed / (i + 1):.2f}s/example)")

    elapsed = time.time() - t0
    print(f"Generation done: {len(eval_records)} examples in {elapsed:.1f}s ({elapsed / len(eval_records):.2f}s/example avg)")

    # Re-write the (possibly limited) eval subset so score_file's
    # line-alignment check passes.
    limited_eval_path = out_dir / "eval_subset.jsonl"
    with open(limited_eval_path, "w", encoding="utf-8") as f:
        for rec in eval_records:
            f.write(json.dumps(rec) + "\n")

    result = score_file(limited_eval_path, pred_path, Path(args.spider_dir))
    print(f"\nExecution accuracy: {result['execution_accuracy']:.4f}  ({result['n_correct']}/{result['n']})")
    (out_dir / "results.json").write_text(json.dumps(result, indent=2))
    print(f"Full results written to {out_dir / 'results.json'}")


if __name__ == "__main__":
    main()
