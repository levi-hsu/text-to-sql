#!/usr/bin/env bash
# BIRD-dev (out-of-distribution) eval for all four arms -- baseline, SFT,
# RL, RL-v2 -- against their already-trained checkpoints. No training
# happens here: this only calls generate_sql.py/eval_sql.py, reusing each
# arm's existing eval config (configs/baseline.yaml, configs/sft_eval.yaml,
# configs/rl_eval.yaml, configs/rl_eval_v2.yaml), which have their bird_*
# fields filled in pointing at data/bird-dev/. Spider-dev is the
# in-distribution set and is not re-run here (see run_baseline_eval.sh /
# run_sft.sh / run_rl_eval.sh / run_rl_eval_v2.sh).
#
# This is the second half of plan.md's generalization measure: the drop
# from Spider-dev to BIRD-dev, per arm. Run scripts/compare_arms.py after
# this to see it computed.
#
# The rl_v2 line below requires runs/rl_qwen2.5coder3b_v2/adapter to exist,
# i.e. scripts/train_rl.py (configs/rl.yaml) and scripts/run_rl_eval_v2.sh
# to have already run. Comment it out if you only want baseline/SFT/RL-v1
# refreshed.
#
# Run from the project root: bash scripts/run_bird_eval.sh

set -euo pipefail

cd "$(dirname "$0")/.."

GOLD="data/bird-dev/dev.sql"
DB_DIR="data/bird-dev/dev_databases"

run_arm () {
    local name="$1"
    local config="$2"
    local run_dir="$3"

    echo "=== [$name] Generating predictions: BIRD-dev ==="
    python scripts/generate_sql.py --config "$config" --dataset bird --split dev

    echo "=== [$name] Scoring execution accuracy: BIRD-dev ==="
    python scripts/eval_sql.py \
        --pred "$run_dir/bird_dev_preds.sql" \
        --gold "$GOLD" \
        --db-dir "$DB_DIR" \
        --output "$run_dir/bird_dev_results.json"
}

run_arm "baseline" "configs/baseline.yaml"  "runs/baseline_qwen2.5coder3b"
run_arm "sft"      "configs/sft_eval.yaml"  "runs/sft_qwen2.5coder3b_eval"
run_arm "rl"       "configs/rl_eval.yaml"   "runs/rl_qwen2.5coder3b_eval"
run_arm "rl_v2"    "configs/rl_eval_v2.yaml" "runs/rl_qwen2.5coder3b_v2_eval"

echo "=== Done. BIRD-dev summaries: ==="
echo "  runs/baseline_qwen2.5coder3b/bird_dev_results.json"
echo "  runs/sft_qwen2.5coder3b_eval/bird_dev_results.json"
echo "  runs/rl_qwen2.5coder3b_eval/bird_dev_results.json"
echo "  runs/rl_qwen2.5coder3b_v2_eval/bird_dev_results.json"
