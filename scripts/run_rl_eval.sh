#!/usr/bin/env bash
# RL arm eval: generate + score Spider-dev predictions for the finished
# GRPO checkpoint (runs/rl_qwen2.5coder3b/adapter, step 300/300). Mirrors
# scripts/run_sft.sh's eval half exactly, so the baseline/SFT/RL comparison
# only varies the adapter being evaluated. Run from the project root:
#   bash scripts/run_rl_eval.sh
#
# BIRD-dev is scored separately for all three models at once by
# scripts/run_bird_eval.sh, against the adapter this script produces -- it
# doesn't retrain anything, so it's kept out of this pipeline.

set -euo pipefail

EVAL_CONFIG="${1:-configs/rl_eval.yaml}"
EVAL_RUN_DIR="runs/rl_qwen2.5coder3b_eval"

cd "$(dirname "$0")/.."

echo "=== Generating RL predictions: Spider-dev ==="
python scripts/generate_sql.py --config "$EVAL_CONFIG" --dataset spider --split dev

echo "=== Scoring execution accuracy: Spider-dev (RL) ==="
python scripts/eval_sql.py \
    --pred "$EVAL_RUN_DIR/spider_dev_preds.sql" \
    --gold data/spider_data/dev_gold.sql \
    --db-dir data/spider_data/database \
    --output "$EVAL_RUN_DIR/spider_dev_results.json"

echo "=== Done. Summary: $EVAL_RUN_DIR/spider_dev_results.json ==="
