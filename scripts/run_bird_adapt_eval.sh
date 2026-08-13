#!/usr/bin/env bash
# Evaluate one Experiment 2 continuation arm (SFT-continue, RL-continue, or
# RL-continue-v2) on all three slices: Spider-dev (retention), pool-heldout
# (same-schema, unseen-question memorization check), crossdb-eval
# (schema-disjoint -- the real transfer test). Run from the project root:
#   bash scripts/run_bird_adapt_eval.sh sft
#   bash scripts/run_bird_adapt_eval.sh rl
#   bash scripts/run_bird_adapt_eval.sh rl_v2
#
# Requires scripts/build_bird_adapt_data.py to have already produced
# data/bird_adapt/, and the corresponding training run (configs/bird_adapt_sft.yaml,
# configs/bird_adapt_rl.yaml, or configs/bird_adapt_rl_v2_phase*.yaml +
# scripts/select_best_bird_adapt_rl_checkpoint.py for rl_v2) to have already
# produced its adapter.
#
# rl_rloo / rl_drgrpo / rl_v2_rloo / rl_v2_drgrpo are the algorithm-swap
# replicates of rl / rl_v2 (same checkpoint, same data -- see
# configs/bird_adapt_rl_rloo.yaml etc. and scripts/run_rl_algo_variants_chain.sh).

set -euo pipefail

ARM="${1:?Usage: bash scripts/run_bird_adapt_eval.sh <sft|rl|rl_v2|rl_rloo|rl_drgrpo|rl_v2_rloo|rl_v2_drgrpo>}"

cd "$(dirname "$0")/.."

case "$ARM" in
  sft)
    POOL_CONFIG="configs/bird_adapt_sft_eval.yaml"
    CROSSDB_CONFIG="configs/bird_adapt_sft_eval_crossdb.yaml"
    RUN_DIR="runs/bird_adapt_sft_eval"
    ;;
  rl)
    POOL_CONFIG="configs/bird_adapt_rl_eval.yaml"
    CROSSDB_CONFIG="configs/bird_adapt_rl_eval_crossdb.yaml"
    RUN_DIR="runs/bird_adapt_rl_eval"
    ;;
  rl_v2)
    POOL_CONFIG="configs/bird_adapt_rl_v2_eval.yaml"
    CROSSDB_CONFIG="configs/bird_adapt_rl_v2_eval_crossdb.yaml"
    RUN_DIR="runs/bird_adapt_rl_v2_eval"
    ;;
  rl_rloo)
    POOL_CONFIG="configs/bird_adapt_rl_rloo_eval.yaml"
    CROSSDB_CONFIG="configs/bird_adapt_rl_rloo_eval_crossdb.yaml"
    RUN_DIR="runs/bird_adapt_rl_rloo_eval"
    ;;
  rl_drgrpo)
    POOL_CONFIG="configs/bird_adapt_rl_drgrpo_eval.yaml"
    CROSSDB_CONFIG="configs/bird_adapt_rl_drgrpo_eval_crossdb.yaml"
    RUN_DIR="runs/bird_adapt_rl_drgrpo_eval"
    ;;
  rl_v2_rloo)
    POOL_CONFIG="configs/bird_adapt_rl_v2_rloo_eval.yaml"
    CROSSDB_CONFIG="configs/bird_adapt_rl_v2_rloo_eval_crossdb.yaml"
    RUN_DIR="runs/bird_adapt_rl_v2_rloo_eval"
    ;;
  rl_v2_drgrpo)
    POOL_CONFIG="configs/bird_adapt_rl_v2_drgrpo_eval.yaml"
    CROSSDB_CONFIG="configs/bird_adapt_rl_v2_drgrpo_eval_crossdb.yaml"
    RUN_DIR="runs/bird_adapt_rl_v2_drgrpo_eval"
    ;;
  *)
    echo "Unknown arm '$ARM' -- expected 'sft', 'rl', 'rl_v2', 'rl_rloo', 'rl_drgrpo', 'rl_v2_rloo', or 'rl_v2_drgrpo'" >&2
    exit 1
    ;;
esac

echo "=== [$ARM] Spider-dev retention check ==="
python scripts/generate_sql.py --config "$POOL_CONFIG" --dataset spider --split dev
python scripts/eval_sql.py \
    --pred "$RUN_DIR/spider_dev_preds.sql" \
    --gold data/spider_data/dev_gold.sql \
    --db-dir data/spider_data/database \
    --output "$RUN_DIR/spider_dev_results.json"

echo "=== [$ARM] Pool-heldout check (same 2 schemas as training, unseen questions) ==="
python scripts/generate_sql.py --config "$POOL_CONFIG" --dataset bird --split dev
python scripts/eval_sql.py \
    --pred "$RUN_DIR/bird_dev_preds.sql" \
    --gold data/bird_adapt/bird_pool_heldout_gold.sql \
    --db-dir data/bird-dev/dev_databases \
    --output "$RUN_DIR/bird_pool_heldout_results.json"

echo "=== [$ARM] Crossdb-eval check (9 dbs disjoint from the training pool -- the transfer test) ==="
python scripts/generate_sql.py --config "$CROSSDB_CONFIG" --dataset bird --split crossdb
python scripts/eval_sql.py \
    --pred "$RUN_DIR/bird_crossdb_preds.sql" \
    --gold data/bird_adapt/bird_crossdb_eval_gold.sql \
    --db-dir data/bird-dev/dev_databases \
    --output "$RUN_DIR/bird_crossdb_results.json"

echo "=== Done. Results under $RUN_DIR/: spider_dev_results.json, bird_pool_heldout_results.json, bird_crossdb_results.json ==="
