#!/usr/bin/env bash
# Runs the RLOO and Dr. GRPO algorithm-swap replicates of RL-continue and
# RL-continue-v2, holding checkpoint and data fixed. See configs/
# bird_adapt_rl_rloo.yaml / bird_adapt_rl_drgrpo.yaml / bird_adapt_rl_v2_phase{1,2}_rloo.yaml
# / bird_adapt_rl_v2_phase{1,2}_drgrpo.yaml headers for the exact field-by-field
# diff against the original GRPO configs (configs/bird_adapt_rl.yaml,
# configs/bird_adapt_rl_v2_phase{1,2}.yaml) -- only `algo` and the output/
# monitor paths differ; model, data, and every grpo.* hyperparameter are
# copied verbatim.
#
# For each algo in {rloo, drgrpo} this runs 2*2 = 4 training jobs total:
#   1. RL-continue analog (configs/bird_adapt_rl_<algo>.yaml): single
#      train_rl.py run on the full 122-row pool
#      (data/bird_adapt/bird_train_pool.jsonl), starting from
#      runs/sft_qwen2.5coder3b/adapter -- the SAME starting checkpoint as
#      the original GRPO run.
#   2. RL-continue-v2 analog: phase 1
#      (configs/bird_adapt_rl_v2_phase1_<algo>.yaml, curriculum warm-up on
#      data/bird_adapt/bird_train_pool_reachable.jsonl -- the SAME
#      bucket_bird_pool_by_difficulty.py output the GRPO v2 run used,
#      NOT regenerated here, since that bucketing only depends on the
#      shared starting checkpoint, not on which RL algorithm trains on it
#      afterward), then phase 2 (configs/bird_adapt_rl_v2_phase2_<algo>.yaml,
#      continues from phase 1's own adapter, full 122-row pool), then
#      checkpoint selection against pool_heldout only
#      (scripts/select_best_bird_adapt_rl_checkpoint.py).
# Then evaluates both new adapters per algo on all three slices
# (spider_retention, pool_heldout, crossdb_transfer), and finally reruns
# scripts/compare_bird_adapt.py once at the end, which now reports all 7
# continuation models together (sft-continue, rl-continue, rl-continue-v2,
# and the 4 new algo-swap models).
#
# Does NOT touch scripts/bucket_bird_pool_by_difficulty.py or either of
# data/bird_adapt/bird_train_pool.jsonl / bird_train_pool_reachable.jsonl --
# same data, same starting checkpoint as the original two experiments, only
# the RL algorithm changes. Does NOT touch sft-continue, rl-continue (v1),
# or rl-continue-v2 (GRPO) -- all three already have results on disk and are
# left alone.
#
# PREREQUISITE, read before launching: your installed trl must support
# GRPOConfig's scale_rewards/loss_type kwargs (for dr_grpo) and
# RLOOTrainer/RLOOConfig (for rloo). Neither has been run end-to-end in this
# project before -- see scripts/train_rl.py's algo=="dr_grpo" and
# algo=="rloo" branches for exactly what each needs, how each fails with an
# actionable error message if your trl is too old, and the one known
# soft-failure mode (RLHealthCallback logging fewer fields for rloo if TRL
# logs its metrics under different key names -- this does not crash
# training, see rl_health_callback.py). Expect to debug the first few
# minutes of each new algo's first run, the same way GRPOTrainer itself
# needed debugging on this project's very first real RL run.
#
# Usage: from the project root, same environment active
# (e.g. `conda activate text-to-sql` first), launched detached so it
# survives you logging out or closing the terminal:
#
#   nohup bash scripts/run_rl_algo_variants_chain.sh > runs/rl_algo_variants_chain.log 2>&1 &
#   disown
#
# Check progress later with:   tail -f runs/rl_algo_variants_chain.log
# Check it's still running with:  pgrep -af run_rl_algo_variants_chain.sh

set -euo pipefail
cd "$(dirname "$0")/.."

for ALGO in rloo drgrpo; do
  echo "[$(date)] ===== algo=$ALGO ====="

  echo "[$(date)] === python scripts/train_rl.py --config configs/bird_adapt_rl_${ALGO}.yaml (RL-continue analog) ==="
  python scripts/train_rl.py --config "configs/bird_adapt_rl_${ALGO}.yaml"

  echo "[$(date)] === python scripts/train_rl.py --config configs/bird_adapt_rl_v2_phase1_${ALGO}.yaml ==="
  python scripts/train_rl.py --config "configs/bird_adapt_rl_v2_phase1_${ALGO}.yaml"

  echo "[$(date)] === python scripts/train_rl.py --config configs/bird_adapt_rl_v2_phase2_${ALGO}.yaml ==="
  python scripts/train_rl.py --config "configs/bird_adapt_rl_v2_phase2_${ALGO}.yaml"

  echo "[$(date)] === python scripts/select_best_bird_adapt_rl_checkpoint.py --run-dir runs/bird_adapt_rl_v2_${ALGO} ==="
  python scripts/select_best_bird_adapt_rl_checkpoint.py --run-dir "runs/bird_adapt_rl_v2_${ALGO}"

  echo "[$(date)] === bash scripts/run_bird_adapt_eval.sh rl_${ALGO} ==="
  bash scripts/run_bird_adapt_eval.sh "rl_${ALGO}"

  echo "[$(date)] === bash scripts/run_bird_adapt_eval.sh rl_v2_${ALGO} ==="
  bash scripts/run_bird_adapt_eval.sh "rl_v2_${ALGO}"

  echo "[$(date)] ===== algo=$ALGO done ====="
done

echo "[$(date)] === python scripts/compare_bird_adapt.py ==="
python scripts/compare_bird_adapt.py

echo "[$(date)] RL algorithm-variant chain (RLOO + Dr. GRPO, continue + continue-v2) finished successfully."
