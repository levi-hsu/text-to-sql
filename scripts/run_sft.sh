#!/usr/bin/env bash
# SFT arm end-to-end: build training subset, QLoRA fine-tune, evaluate on
# Spider-dev. Run from the project root: bash scripts/run_sft.sh
#
# BIRD-dev is scored separately for all three models at once by
# scripts/run_bird_eval.sh, against the adapter this script produces -- it
# doesn't retrain anything, so it's kept out of this pipeline.

set -euo pipefail

TRAIN_CONFIG="${1:-configs/sft.yaml}"
EVAL_CONFIG="${2:-configs/sft_eval.yaml}"
EVAL_RUN_DIR="runs/sft_qwen2.5coder3b_eval"

cd "$(dirname "$0")/.."

echo "=== Building SFT training data (filtered Spider-train subset) ==="
python scripts/build_sft_data.py --config "$TRAIN_CONFIG"

echo "=== Training SFT (QLoRA) ==="
python scripts/train_sft.py --config "$TRAIN_CONFIG"

echo "=== Generating SFT predictions: Spider-dev ==="
python scripts/generate_sql.py --config "$EVAL_CONFIG" --dataset spider --split dev

echo "=== Scoring execution accuracy: Spider-dev (SFT) ==="
python scripts/eval_sql.py \
    --pred "$EVAL_RUN_DIR/spider_dev_preds.sql" \
    --gold data/spider_data/dev_gold.sql \
    --db-dir data/spider_data/database \
    --output "$EVAL_RUN_DIR/spider_dev_results.json"

echo "=== Done. Summary: $EVAL_RUN_DIR/spider_dev_results.json ==="
