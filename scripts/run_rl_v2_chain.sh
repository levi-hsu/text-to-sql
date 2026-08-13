#!/usr/bin/env bash
# Runs the full RL-continue-v2 curriculum pipeline in order, stopping
# immediately if any step fails (set -e):
#   1. bucket the 122-example BIRD pool by difficulty under the shared
#      starting checkpoint
#   2. phase 1 RL: curriculum warm-up on the "reachable" (mixed-difficulty)
#      subset only
#   3. phase 2 RL: continue from phase 1's adapter, train on the full pool
#   4. score every saved checkpoint against pool_heldout ONLY and promote
#      the winner into runs/bird_adapt_rl_v2/adapter
#   5. evaluate the selected checkpoint on all three slices
#   6. print the sft-continue / rl-continue / rl-continue-v2 comparison
#
# Does not touch sft-continue or rl-continue (v1) -- both already have
# results on disk and are left alone, per your instruction not to re-run them.
#
# Usage: from the project root, with the same environment active
# (e.g. `conda activate text-to-sql` first), launch detached so it survives
# you logging out or closing the terminal:
#
#   nohup bash scripts/run_rl_v2_chain.sh > runs/rl_v2_chain.log 2>&1 &
#   disown
#
# Check progress later with:   tail -f runs/rl_v2_chain.log
# Check it's still running with:  pgrep -af run_rl_v2_chain.sh

set -euo pipefail
cd "$(dirname "$0")/.."

echo "[$(date)] === python scripts/bucket_bird_pool_by_difficulty.py ==="
python scripts/bucket_bird_pool_by_difficulty.py

echo "[$(date)] === python scripts/train_rl.py --config configs/bird_adapt_rl_v2_phase1.yaml ==="
python scripts/train_rl.py --config configs/bird_adapt_rl_v2_phase1.yaml

echo "[$(date)] === python scripts/train_rl.py --config configs/bird_adapt_rl_v2_phase2.yaml ==="
python scripts/train_rl.py --config configs/bird_adapt_rl_v2_phase2.yaml

echo "[$(date)] === python scripts/select_best_bird_adapt_rl_checkpoint.py ==="
python scripts/select_best_bird_adapt_rl_checkpoint.py

echo "[$(date)] === bash scripts/run_bird_adapt_eval.sh rl_v2 ==="
bash scripts/run_bird_adapt_eval.sh rl_v2

echo "[$(date)] === python scripts/compare_bird_adapt.py ==="
python scripts/compare_bird_adapt.py

echo "[$(date)] RL-continue-v2 pipeline finished successfully."
