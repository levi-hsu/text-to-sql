#!/usr/bin/env bash
# Baseline arm end-to-end: zero-shot Qwen2.5-Coder-3B-Instruct on Spider-dev.
# Run from the project root: bash scripts/run_baseline_eval.sh
#
# BIRD-dev is scored separately for all three arms at once by
# scripts/run_bird_eval.sh (no retraining involved, so it doesn't belong
# inside this or run_sft.sh's pipeline).

set -euo pipefail

CONFIG="${1:-configs/baseline.yaml}"
RUN_DIR="runs/baseline_qwen2.5coder3b"

cd "$(dirname "$0")/.."

echo "=== Generating predictions: Spider-dev ==="
python scripts/generate_sql.py --config "$CONFIG" --dataset spider --split dev

echo "=== Scoring execution accuracy: Spider-dev ==="
python scripts/eval_sql.py \
    --pred "$RUN_DIR/spider_dev_preds.sql" \
    --gold data/spider_data/dev_gold.sql \
    --db-dir data/spider_data/database \
    --output "$RUN_DIR/spider_dev_results.json"

echo "=== Done. Summary: $RUN_DIR/spider_dev_results.json ==="
