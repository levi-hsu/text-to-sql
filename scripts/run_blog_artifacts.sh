#!/usr/bin/env bash
# Generates every data-backed artifact the text-to-SQL blog post needs, from
# results that already exist on disk. No training and no model loading
# happens here -- every step only reads *_results.json / *_raw.jsonl files
# scripts/generate_sql.py and scripts/eval_sql.py already produced, so this
# is safe and fast to run unattended even though it's launched the same
# detached way as the training chains.
#
# What it does, in order:
#   1. Refreshes the core 4-arm (baseline/SFT/RL/RL-v2) scorecard and error
#      taxonomy on Spider eval and BIRD eval (scripts/compare_arms.py,
#      scripts/compare_error_taxonomy.py). compare_error_taxonomy.py also
#      (re)writes each arm's *_taxonomy.json as a side effect.
#   2. Backfills *_taxonomy.json for Experiment 2's 7 bird_adapt
#      continue arms x 3 slices (Spider eval EX, BIRD continue
#      same-schema EX, BIRD continue cross-database EX) --
#      scripts/compare_bird_adapt.py only
#      reports accuracy, it never called scripts/error_taxonomy.py, so
#      these 21 files don't exist yet. Then reruns
#      scripts/compare_bird_adapt.py for the refreshed accuracy scorecard.
#   3. Renders real gold-vs-predicted example galleries (markdown, one
#      section per error category, real question + real executed result
#      rows) via scripts/select_blog_examples.py, for the four core arms
#      on Spider eval and for SFT/RL-v2 on BIRD eval -- the generalization
#      comparison plan.md's "Metrics" section calls out.
#   4. Regenerates both blog figures (scripts/make_blog_figures.py), since
#      their legends are baked-in images, not text, and need to match
#      whatever display labels the tables above use.
#
# NOTE on naming: "Spider eval" / "BIRD eval" / "BIRD continue
# same-schema EX" / etc. are display labels only, used in printed
# output, gallery filenames, and
# draft.md -- not renames of the underlying data files, which stay at
# their existing paths (data/spider_data/, data/bird-dev/, etc.) so no
# config or the actual training/eval pipeline needs to change.
#
# Everything is written under runs/blog_artifacts/ (tables, galleries) and
# blog/figures/ (the two PNGs).
#
# Usage: from the project root, same environment active
# (e.g. `conda activate text-to-sql` first), launched detached so it
# survives you logging out or closing the terminal:
#
#   nohup bash scripts/run_blog_artifacts.sh > runs/blog_artifacts_chain.log 2>&1 &
#   disown
#
# Check progress later with:   tail -f runs/blog_artifacts_chain.log
# Check it's still running with:  pgrep -af run_blog_artifacts.sh

set -euo pipefail
cd "$(dirname "$0")/.."

OUT_DIR="runs/blog_artifacts"
mkdir -p "$OUT_DIR"

echo "[$(date)] === python scripts/compare_arms.py ==="
python scripts/compare_arms.py | tee "$OUT_DIR/experiment1_scorecard.txt"

echo "[$(date)] === python scripts/compare_error_taxonomy.py ==="
python scripts/compare_error_taxonomy.py | tee "$OUT_DIR/experiment1_error_taxonomy.txt"

echo "[$(date)] === backfilling Experiment 2 (bird_adapt) error taxonomy, 7 arms x 3 slices ==="
BIRD_ADAPT_ARMS=(
  bird_adapt_sft_eval
  bird_adapt_rl_eval
  bird_adapt_rl_v2_eval
  bird_adapt_rl_rloo_eval
  bird_adapt_rl_drgrpo_eval
  bird_adapt_rl_v2_rloo_eval
  bird_adapt_rl_v2_drgrpo_eval
)
for RUN_DIR in "${BIRD_ADAPT_ARMS[@]}"; do
  for RESULTS in spider_dev_results.json bird_pool_heldout_results.json bird_crossdb_results.json; do
    SRC="runs/$RUN_DIR/$RESULTS"
    if [[ -f "$SRC" ]]; then
      DST="${SRC/_results.json/_taxonomy.json}"
      echo "  -> $DST"
      python scripts/error_taxonomy.py --results "$SRC" --output "$DST" --label "$RUN_DIR/$RESULTS" > /dev/null
    else
      echo "  -- skipping $SRC, not found --"
    fi
  done
done

echo "[$(date)] === python scripts/compare_bird_adapt.py ==="
python scripts/compare_bird_adapt.py | tee "$OUT_DIR/experiment2_scorecard.txt"

echo "[$(date)] === rendering example galleries: scripts/select_blog_examples.py ==="

gallery () {
  local taxonomy="$1" raw="$2" db_dir="$3" label="$4" out="$5"
  echo "  -> $out"
  python scripts/select_blog_examples.py \
    --taxonomy "$taxonomy" --raw "$raw" --db-dir "$db_dir" \
    --label "$label" --out "$OUT_DIR/$out"
}

# Core 4-arm comparison on Spider eval (in-distribution) -- the main
# "tour of failure modes across post-training methods" material.
# Filenames say "spider_eval"/"bird_eval", matching draft.md's citations --
# renamed from the earlier "*_dev_gallery.md" pass, see stale-file cleanup
# at the end of this script.
gallery runs/baseline_qwen2.5coder3b/spider_dev_taxonomy.json \
        runs/baseline_qwen2.5coder3b/spider_dev_raw.jsonl \
        data/spider_data/database "Baseline, Spider eval" baseline_spider_eval_gallery.md

gallery runs/sft_qwen2.5coder3b_eval/spider_dev_taxonomy.json \
        runs/sft_qwen2.5coder3b_eval/spider_dev_raw.jsonl \
        data/spider_data/database "SFT, Spider eval" sft_spider_eval_gallery.md

gallery runs/rl_qwen2.5coder3b_eval/spider_dev_taxonomy.json \
        runs/rl_qwen2.5coder3b_eval/spider_dev_raw.jsonl \
        data/spider_data/database "RL, Spider eval" rl_spider_eval_gallery.md

gallery runs/rl_qwen2.5coder3b_v2_eval/spider_dev_taxonomy.json \
        runs/rl_qwen2.5coder3b_v2_eval/spider_dev_raw.jsonl \
        data/spider_data/database "RL-v2, Spider eval" rl_v2_spider_eval_gallery.md

# SFT and RL-v2 on BIRD eval (out-of-distribution) -- the generalization
# side of the story.
gallery runs/sft_qwen2.5coder3b_eval/bird_dev_taxonomy.json \
        runs/sft_qwen2.5coder3b_eval/bird_dev_raw.jsonl \
        data/bird-dev/dev_databases "SFT, BIRD eval" sft_bird_eval_gallery.md

gallery runs/rl_qwen2.5coder3b_v2_eval/bird_dev_taxonomy.json \
        runs/rl_qwen2.5coder3b_v2_eval/bird_dev_raw.jsonl \
        data/bird-dev/dev_databases "RL-v2, BIRD eval" rl_v2_bird_eval_gallery.md

echo "[$(date)] === removing stale pre-rename gallery files, if any (best-effort, not fatal) ==="
rm -f "$OUT_DIR"/baseline_spider_dev_gallery.md "$OUT_DIR"/sft_spider_dev_gallery.md \
      "$OUT_DIR"/rl_spider_dev_gallery.md "$OUT_DIR"/rl_v2_spider_dev_gallery.md \
      "$OUT_DIR"/sft_bird_dev_gallery.md "$OUT_DIR"/rl_v2_bird_dev_gallery.md 2>/dev/null || true

echo "[$(date)] === python scripts/make_blog_figures.py ==="
python scripts/make_blog_figures.py
rm -f blog/figures/fig2_error_mix_spider_dev.png 2>/dev/null || true   # superseded by fig2_error_mix_spider_eval.png

echo "[$(date)] Blog artifact pipeline finished successfully. See $OUT_DIR/ and blog/figures/"
