#!/usr/bin/env bash
# Waits for the already-running `train_sft.py --config configs/bird_adapt_sft.yaml`
# to finish, then runs the rest of Experiment 2 in order, stopping immediately
# if any step fails (set -e). Does NOT touch the running train_sft.py process,
# only polls for it -- safe to launch from a second terminal while the first
# keeps training.
#
# Usage: run this from a NEW terminal (leave the train_sft.py one alone),
# with the same environment active (e.g. `conda activate text-to-sql` first),
# then detach it so it survives you logging out or closing the terminal:
#
#   nohup bash scripts/run_experiment2_chain.sh > runs/experiment2_chain.log 2>&1 &
#   disown
#
# Check progress later with:  tail -f runs/experiment2_chain.log
# Check it's still running with:  pgrep -af run_experiment2_chain.sh

set -euo pipefail
cd "$(dirname "$0")/.."

TRAIN_SFT_PATTERN="train_sft.py --config configs/bird_adapt_sft.yaml"

echo "[$(date)] Waiting for '$TRAIN_SFT_PATTERN' to finish..."
while pgrep -f "$TRAIN_SFT_PATTERN" > /dev/null; do
    sleep 30
done
echo "[$(date)] train_sft.py is done. Continuing with the rest of the pipeline."

echo "[$(date)] === bash scripts/run_bird_adapt_eval.sh sft ==="
bash scripts/run_bird_adapt_eval.sh sft

echo "[$(date)] === python scripts/train_rl.py --config configs/bird_adapt_rl.yaml ==="
python scripts/train_rl.py --config configs/bird_adapt_rl.yaml

echo "[$(date)] === bash scripts/run_bird_adapt_eval.sh rl ==="
bash scripts/run_bird_adapt_eval.sh rl

echo "[$(date)] === python scripts/compare_bird_adapt.py ==="
python scripts/compare_bird_adapt.py

echo "[$(date)] Experiment 2 pipeline finished successfully."
