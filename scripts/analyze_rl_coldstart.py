"""Was RL-continue's pool-heldout collapse (1/31) a regression, or did it
just fail to move a starting point that was already bad? Reconstructs the
"before" accuracy of baseline/SFT(spider-only)/RL-v2(spider-only) on the
EXACT SAME questions used in bird_adapt's pool_heldout and crossdb_eval
splits, by joining eval_sql.py's original bird_dev_results.json (db_id,
gold_sql, match) against build_bird_adapt_data.py's split files on
(db_id, gold_sql) -- gold_sql text is reproduced identically in both,
since both are ultimately rendered from the same dev.json "SQL" field with
the same trailing-semicolon-stripped convention (see
build_bird_adapt_data.py's write_dev_and_gold).

This is the "cold start" check the GRPO/RLVR literature on reward sparsity
would predict matters: if the SFT-on-Spider starting checkpoint already
scored near-zero on these 31 pool-heldout questions before any BIRD-slice
training, RL-continue's 0.0323 is "barely moved from an already-bad
starting point," not "made things worse." If the starting checkpoint
scored comparably to its overall BIRD rate (~19-20%) on these same 31
questions, RL-continue's 0.0323 is a real, large regression.

Usage:
  python scripts/analyze_rl_coldstart.py
"""

import json
import os

ORIGINAL_ARMS = [
    ("baseline", "runs/baseline_qwen2.5coder3b/bird_dev_results.json"),
    ("sft (spider-only)", "runs/sft_qwen2.5coder3b_eval/bird_dev_results.json"),
    ("rl_v2 (spider-only)", "runs/rl_qwen2.5coder3b_v2_eval/bird_dev_results.json"),
]

NEW_ARMS = [
    ("sft-continue", "runs/bird_adapt_sft_eval"),
    ("rl-continue", "runs/bird_adapt_rl_eval"),
]

SPLITS = [
    ("pool_heldout (31 examples, same schemas as training)", "data/bird_adapt/bird_pool_heldout_gold.sql", "bird_pool_heldout_results.json"),
    ("crossdb_eval (schema-disjoint transfer test)", "data/bird_adapt/bird_crossdb_eval_gold.sql", "bird_crossdb_results.json"),
]


def load_gold_keys(gold_sql_path):
    """(db_id, gold_sql_text) pairs from a gold.sql file -- same join key
    space as eval_sql.py's own results.json rows."""
    keys = []
    with open(gold_sql_path) as f:
        for line in f:
            line = line.rstrip("\n")
            if not line.strip():
                continue
            sql, db_id = line.split("\t")
            keys.append((db_id.strip(), sql.strip()))
    return keys


def restricted_accuracy(results_path, keys):
    with open(results_path) as f:
        results = json.load(f)["results"]
    by_key = {}
    for r in results:
        k = (r["db_id"], r["gold_sql"].strip())
        by_key.setdefault(k, []).append(r)

    matched, correct, unmatched = 0, 0, 0
    for k in keys:
        rows = by_key.get(k)
        if not rows:
            unmatched += 1
            continue
        matched += 1
        # if a gold_sql string isn't unique within a db (rare, small dbs),
        # count it correct if ANY matching row is correct -- conservative
        # in the direction of not undercounting the "before" baseline.
        if any(r["match"] for r in rows):
            correct += 1
    return {"matched": matched, "unmatched": unmatched, "correct": correct,
            "accuracy": correct / matched if matched else 0.0}


def new_arm_summary(run_dir, results_filename):
    path = os.path.join(run_dir, results_filename)
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)["summary"]


def main():
    for split_label, gold_path, results_filename in SPLITS:
        print(f"\n{'=' * 70}")
        print(f"SPLIT: {split_label}")
        print("=" * 70)
        keys = load_gold_keys(gold_path)
        print(f"{len(keys)} questions in this split\n")

        print("-- BEFORE (spider-only arms, accuracy on these exact questions) --")
        before = {}
        for name, path in ORIGINAL_ARMS:
            if not os.path.exists(path):
                print(f"  {name:<22}-- missing --")
                continue
            r = restricted_accuracy(path, keys)
            before[name] = r
            note = f" ({r['unmatched']} unmatched)" if r["unmatched"] else ""
            print(f"  {name:<22}matched={r['matched']:>5}  correct={r['correct']:>5}  accuracy={r['accuracy']:.4f}{note}")

        print("\n-- AFTER (continuation arms, on the same split) --")
        for name, run_dir in NEW_ARMS:
            summary = new_arm_summary(run_dir, results_filename)
            if summary is None:
                print(f"  {name:<22}-- not yet evaluated --")
                continue
            print(f"  {name:<22}total={summary['total']:>5}  correct={summary['correct']:>5}  "
                  f"accuracy={summary['execution_accuracy']:.4f}")

        print("\n-- DELTA (after minus each before) --")
        for name, run_dir in NEW_ARMS:
            summary = new_arm_summary(run_dir, results_filename)
            if summary is None:
                continue
            for base_name, b in before.items():
                delta = summary["execution_accuracy"] - b["accuracy"]
                print(f"  {name} vs {base_name:<22}{delta:+.4f}")


if __name__ == "__main__":
    main()
