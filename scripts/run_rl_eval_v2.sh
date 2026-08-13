#!/usr/bin/env bash
# RL v2 arm eval: generate + score Spider-dev predictions for the v2 GRPO
# checkpoint (runs/rl_qwen2.5coder3b_v2/adapter -- see configs/rl.yaml's
# header for what changed from the first RL run). Mirrors
# scripts/run_rl_eval.sh exactly, just pointed at the v2 config/run dir, so
# the two RL runs' results sit side by side rather than overwriting each
# other. Run from the project root:
#   bash scripts/run_rl_eval_v2.sh
#
# BIRD-dev is scored separately for all arms at once by
# scripts/run_bird_eval.sh, against the adapter this script produces -- it
# doesn't retrain anything, so it's kept out of this pipeline.

set -euo pipefail

EVAL_CONFIG="${1:-configs/rl_eval_v2.yaml}"
EVAL_RUN_DIR="runs/rl_qwen2.5coder3b_v2_eval"

cd "$(dirname "$0")/.."

echo "=== Generating RL-v2 predictions: Spider-dev ==="
python scripts/generate_sql.py --config "$EVAL_CONFIG" --dataset spider --split dev

echo "=== Scoring execution accuracy: Spider-dev (RL-v2) ==="
python scripts/eval_sql.py \
    --pred "$EVAL_RUN_DIR/spider_dev_preds.sql" \
    --gold data/spider_data/dev_gold.sql \
    --db-dir data/spider_data/database \
    --output "$EVAL_RUN_DIR/spider_dev_results.json"

echo "=== Done. Summary: $EVAL_RUN_DIR/spider_dev_results.json ==="
