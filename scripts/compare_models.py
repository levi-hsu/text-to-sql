"""Print a side-by-side execution-accuracy comparison across all models
(baseline, SFT, RL, RL-v2) on both Spider eval (in-distribution) and
BIRD eval (out-of-distribution), reading each arm's eval_sql.py summary
directly rather than re-deriving numbers by hand. Also prints the
Spider-to-BIRD accuracy drop per arm -- the generalization measure
plan.md's "Metrics" section defines: a smaller drop means better
generalization from that post-training method.

NOTE on naming: "Spider eval" / "BIRD eval" are display labels only, used
in the printed output below. The underlying files are still
data/spider_data/dev.json and data/bird-dev/dev.json (Spider's and BIRD's
own "dev" split), and the *_dev_results.json paths in models below are left
unchanged -- renaming those would mean renaming actual run directories and
every config that references them. Only what gets printed changed, because
"dev" reads as "used to tune something" when both are actually the only
number ever reported for that arm on that set.

rl_v2 is the second RL run (configs/rl.yaml): started from the SFT arm's
checkpoint-500 instead of its fully-converged final adapter,
num_generations raised 2->4, and a small partial-credit reward term added
on top of the unchanged execution-match objective -- see configs/rl.yaml's
header comment for the full rationale. rl (v1) scored Spider eval EX
identical to SFT (0.6973, bit-for-bit the same predictions on 1021/1034
examples) and a worse Spider->BIRD drop than baseline; rl_v2 exists to test
whether those three changes fix that.

Usage:
  python scripts/compare_models.py
"""

import json
import os

# Display labels only -- the internal keys ("spider", "bird") used to index
# `rows` below are unchanged, so this map is the only thing that needs to
# change if the display naming changes again.
SET_LABELS = {"spider": "Spider eval", "bird": "BIRD eval"}

models = [
    (
        "baseline",
        "runs/baseline_qwen2.5coder3b/spider_dev_results.json",
        "runs/baseline_qwen2.5coder3b/bird_dev_results.json",
    ),
    (
        "sft",
        "runs/sft_qwen2.5coder3b_eval/spider_dev_results.json",
        "runs/sft_qwen2.5coder3b_eval/bird_dev_results.json",
    ),
    (
        "rl",
        "runs/rl_qwen2.5coder3b_eval/spider_dev_results.json",
        "runs/rl_qwen2.5coder3b_eval/bird_dev_results.json",
    ),
    (
        "rl_v2",
        "runs/rl_qwen2.5coder3b_v2_eval/spider_dev_results.json",
        "runs/rl_qwen2.5coder3b_v2_eval/bird_dev_results.json",
    ),
]


def load_summary(path):
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)["summary"]


def print_delta(label, a, b):
    """a - b, both possibly-None execution_accuracy summaries."""
    if a and b:
        print(f"{label:<16}{a['execution_accuracy'] - b['execution_accuracy']:+.4f}")


def main():
    rows = {}
    for name, spider_path, bird_path in models:
        rows[name] = {
            "spider": load_summary(spider_path),
            "bird": load_summary(bird_path),
        }

    header = f"{'arm':<10}{'set':<12}{'total':>8}{'correct':>10}{'exec_acc':>12}{'pred_err':>10}"
    print(header)
    print("-" * len(header))
    for name, _, _ in models:
        for set_name in ("spider", "bird"):
            summary = rows[name][set_name]
            label = SET_LABELS[set_name]
            if summary is None:
                print(f"{name:<10}{label:<12}{'-- not yet evaluated --':>40}")
                continue
            print(
                f"{name:<10}"
                f"{label:<12}"
                f"{summary['total']:>8}"
                f"{summary['correct']:>10}"
                f"{summary['execution_accuracy']:>12.4f}"
                f"{summary['pred_execution_errors']:>10}"
            )

    baseline_spider = rows["baseline"]["spider"]
    sft_spider = rows["sft"]["spider"]
    rl_spider = rows["rl"]["spider"]
    rl_v2_spider = rows["rl_v2"]["spider"]

    print("\n-- Spider eval deltas (in-distribution) --")
    print_delta("SFT - baseline:", sft_spider, baseline_spider)
    print_delta("RL - SFT:", rl_spider, sft_spider)
    print_delta("RL - baseline:", rl_spider, baseline_spider)
    print_delta("RL-v2 - SFT:", rl_v2_spider, sft_spider)
    print_delta("RL-v2 - RL:", rl_v2_spider, rl_spider)

    baseline_bird = rows["baseline"]["bird"]
    sft_bird = rows["sft"]["bird"]
    rl_bird = rows["rl"]["bird"]
    rl_v2_bird = rows["rl_v2"]["bird"]

    print("\n-- BIRD eval deltas (out-of-distribution) --")
    print_delta("SFT - baseline:", sft_bird, baseline_bird)
    print_delta("RL - SFT:", rl_bird, sft_bird)
    print_delta("RL-v2 - SFT:", rl_v2_bird, sft_bird)
    print_delta("RL-v2 - RL:", rl_v2_bird, rl_bird)
    print_delta("RL-v2 - baseline:", rl_v2_bird, baseline_bird)

    print("\n-- Spider eval -> BIRD eval drop per arm (generalization measure, plan.md) --")
    print("Smaller drop = better generalization from that post-training method.")
    for name, _, _ in models:
        spider = rows[name]["spider"]
        bird = rows[name]["bird"]
        if spider is None or bird is None:
            print(f"{name:<10}-- missing Spider eval or BIRD eval result, run scripts/run_bird_eval.sh --")
            continue
        drop = spider["execution_accuracy"] - bird["execution_accuracy"]
        print(
            f"{name:<10}spider_eval={spider['execution_accuracy']:.4f}  "
            f"bird_eval={bird['execution_accuracy']:.4f}  drop={drop:+.4f}"
        )


if __name__ == "__main__":
    main()
