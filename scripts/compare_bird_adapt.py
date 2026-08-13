"""Experiment 2 comparison: SFT-continue vs RL-continue, both
starting from the same Spider-only SFT checkpoint, both given the same
small BIRD eval training pool, carved out of BIRD eval itself rather than
from BIRD train's official split (scripts/build_bird_adapt_data.py) --
BIRD train remains entirely unused throughout this project.

Reports three slices per continue arm, display names matching draft.md:
  Spider eval EX               (spider_retention key below) : did
                                continuing on BIRD forget Spider competence?
  BIRD continue same-schema EX (pool_heldout key below)      : same 2
                                schemas as training, unseen questions --
                                memorization check.
  BIRD continue cross-database EX (crossdb_transfer key below) : the 7
                                databases entirely disjoint from the
                                training pool -- the actual generalization
                                -transfer test, and the number that answers
                                Experiment 2's question. (Not 9 -- an
                                earlier comment in run_bird_adapt_eval.sh
                                said 9, but bird_crossdb_eval_dev.json
                                actually resolves to 7 unique db_ids,
                                confirmed by the db_ids print below, which
                                is computed from the real file, not
                                hardcoded.)

Also recomputes the original baseline/SFT(spider-only)/RL-v2(spider-only)
execution accuracy RESTRICTED to just the crossdb-eval db_ids -- not their
full 1534-example BIRD eval number, which includes the two pool databases
and would not be a fair comparison against crossdb_transfer's 1381-example
set. This is the apples-to-apples baseline the new models need to beat.

Usage:
  python scripts/compare_bird_adapt.py
"""

import json
import os

CROSSDB_DEV = "data/bird_adapt/bird_crossdb_eval_dev.json"

# Display labels only, paired 1:1 with SLICES' internal (slice_key, fname)
# below -- matches the renaming applied to draft.md and its case studies.
SLICE_LABELS = {
    "spider_retention": "Spider eval EX",
    "pool_heldout": "BIRD continue same-schema EX",
    "crossdb_transfer": "BIRD continue cross-database EX",
}

ORIGINAL_models = [
    ("baseline", "runs/baseline_qwen2.5coder3b/bird_dev_results.json"),
    ("sft (spider-only)", "runs/sft_qwen2.5coder3b_eval/bird_dev_results.json"),
    ("rl_v2 (spider-only)", "runs/rl_qwen2.5coder3b_v2_eval/bird_dev_results.json"),
]

NEW_models = [
    ("sft-continue", "runs/bird_adapt_sft_eval"),
    ("rl-continue", "runs/bird_adapt_rl_eval"),
    ("rl-continue-v2", "runs/bird_adapt_rl_v2_eval"),
    # Algorithm-swap replicates -- same checkpoint, same data as the two models
    # above, only the RL algorithm differs (see
    # scripts/run_rl_algo_variants_chain.sh and configs/bird_adapt_rl_rloo.yaml
    # / _drgrpo.yaml / bird_adapt_rl_v2_phase{1,2}_rloo.yaml / _drgrpo.yaml).
    ("rl-continue-rloo", "runs/bird_adapt_rl_rloo_eval"),
    ("rl-continue-drgrpo", "runs/bird_adapt_rl_drgrpo_eval"),
    ("rl-continue-v2-rloo", "runs/bird_adapt_rl_v2_rloo_eval"),
    ("rl-continue-v2-drgrpo", "runs/bird_adapt_rl_v2_drgrpo_eval"),
]

SLICES = [
    ("spider_retention", "spider_dev_results.json"),
    ("pool_heldout", "bird_pool_heldout_results.json"),
    ("crossdb_transfer", "bird_crossdb_results.json"),
]


def crossdb_db_ids():
    if not os.path.exists(CROSSDB_DEV):
        raise FileNotFoundError(
            f"{CROSSDB_DEV} not found -- run scripts/build_bird_adapt_data.py first."
        )
    with open(CROSSDB_DEV) as f:
        examples = json.load(f)
    return {ex["db_id"] for ex in examples}


def restricted_accuracy(results_path, db_ids):
    with open(results_path) as f:
        results = json.load(f)["results"]
    subset = [r for r in results if r["db_id"] in db_ids]
    correct = sum(1 for r in subset if r["match"])
    total = len(subset)
    return {"total": total, "correct": correct, "execution_accuracy": correct / total if total else 0.0}


def load_summary(path):
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)["summary"]


def main():
    db_ids = crossdb_db_ids()
    print(f"crossdb-eval db_ids ({len(db_ids)}): {sorted(db_ids)}\n")

    print("=== Original models, RESTRICTED to the crossdb-eval db_ids (apples-to-apples baseline) ===")
    restricted = {}
    for name, path in ORIGINAL_models:
        if not os.path.exists(path):
            print(f"{name:<24}-- missing --")
            continue
        r = restricted_accuracy(path, db_ids)
        restricted[name] = r
        print(f"{name:<24}total={r['total']:>5}  correct={r['correct']:>5}  exec_acc={r['execution_accuracy']:.4f}")

    print("\n=== New continue models, all three slices ===")
    header = f"{'arm':<24}{'slice':<34}{'total':>8}{'correct':>10}{'exec_acc':>12}"
    print(header)
    print("-" * len(header))
    for name, run_dir in NEW_models:
        for slice_name, fname in SLICES:
            label = SLICE_LABELS[slice_name]
            path = os.path.join(run_dir, fname)
            summary = load_summary(path)
            if summary is None:
                print(f"{name:<24}{label:<34}{'-- not yet evaluated --':>30}")
                continue
            print(
                f"{name:<24}{label:<34}{summary['total']:>8}"
                f"{summary['correct']:>10}{summary['execution_accuracy']:>12.4f}"
            )

    print("\n=== The actual question: does either continue arm beat the spider-only models on the SAME cross-database questions? ===")
    for name, run_dir in NEW_models:
        path = os.path.join(run_dir, "bird_crossdb_results.json")
        summary = load_summary(path)
        if summary is None:
            print(f"{name:<16}-- BIRD continue cross-database EX not yet evaluated --")
            continue
        print(f"{name}: BIRD continue cross-database EX = {summary['execution_accuracy']:.4f}")
        for base_name, base in restricted.items():
            delta = summary["execution_accuracy"] - base["execution_accuracy"]
            print(f"  vs {base_name:<24}{delta:+.4f}")


if __name__ == "__main__":
    main()
