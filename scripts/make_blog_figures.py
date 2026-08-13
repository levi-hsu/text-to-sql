"""Generates the two figures the blog post embeds, from the same numbers
already printed by compare_arms.py and compare_error_taxonomy.py (copied
here as constants rather than re-parsed from their text output, so this
stays a simple, auditable script -- if those numbers change, update both
places, or diff runs/blog_artifacts/experiment1_scorecard.txt and
experiment1_error_taxonomy.txt against the constants below before trusting
a regenerated figure).

fig1: execution accuracy by arm, Spider eval vs BIRD eval -- the
      generalization-drop finding.
fig2: Spider eval error-category mix by arm -- the structural-errors-down,
      value-level-errors-up finding.

Was previously generated ad hoc and not checked into the repo; this is
that same code, saved so `scripts/run_blog_artifacts.sh` can regenerate
both figures alongside the tables and galleries whenever the underlying
numbers change.

Usage:
  python scripts/make_blog_figures.py
Output:
  blog/figures/fig1_execution_accuracy_by_arm.png
  blog/figures/fig2_error_mix_spider_eval.png
"""

import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

OUT_DIR = "blog/figures"

plt.rcParams.update({"font.size": 11, "axes.spines.top": False, "axes.spines.right": False})


def make_fig1():
    arms = ["baseline", "SFT", "RL", "RL-v2"]
    spider_eval = [0.6083, 0.6973, 0.6973, 0.7128]
    bird_eval = [0.2073, 0.1877, 0.1956, 0.1851]

    x = np.arange(len(arms))
    w = 0.35
    fig, ax = plt.subplots(figsize=(6.5, 4.2))
    b1 = ax.bar(x - w / 2, spider_eval, w, label="Spider eval (in-distribution)", color="#2f6fed")
    b2 = ax.bar(x + w / 2, bird_eval, w, label="BIRD eval (out-of-distribution)", color="#e8743b")
    ax.set_xticks(x)
    ax.set_xticklabels(arms)
    ax.set_ylabel("Execution accuracy")
    ax.set_ylim(0, 0.88)
    ax.set_title("Execution accuracy by arm")
    ax.legend(frameon=False, loc="upper center", ncol=1, bbox_to_anchor=(0.27, 1.0))
    for bars in (b1, b2):
        for rect in bars:
            h = rect.get_height()
            ax.annotate(f"{h:.3f}", (rect.get_x() + rect.get_width() / 2, h),
                        textcoords="offset points", xytext=(0, 3), ha="center", fontsize=9)
    fig.tight_layout()
    out_path = os.path.join(OUT_DIR, "fig1_execution_accuracy_by_arm.png")
    fig.savefig(out_path, dpi=150)
    print(f"wrote {out_path}")


def make_fig2():
    categories = ["schema_column_error", "join_structure_mismatch", "other_wrong_result"]
    data = {
        "baseline": [0.3325, 0.2705, 0.2705],
        "SFT": [0.2379, 0.2186, 0.3473],
        "RL": [0.2476, 0.2219, 0.3376],
        "RL-v2": [0.2542, 0.2102, 0.3661],
    }
    arms = list(data.keys())
    x = np.arange(len(categories))
    w = 0.2
    colors = ["#8a8f98", "#2f6fed", "#3fb27f", "#e8743b"]
    fig, ax = plt.subplots(figsize=(7.5, 4.2))
    for i, arm in enumerate(arms):
        ax.bar(x + (i - 1.5) * w, data[arm], w, label=arm, color=colors[i])
    ax.set_xticks(x)
    ax.set_xticklabels(["schema column\nerror", "join structure\nmismatch", "other wrong\nresult"])
    ax.set_ylabel("Rate among that arm's\nincorrect predictions")
    ax.set_title("Spider eval error mix: structural errors down, value-level errors up")
    ax.legend(frameon=False, ncol=4, loc="upper center", bbox_to_anchor=(0.5, -0.12))
    fig.tight_layout()
    out_path = os.path.join(OUT_DIR, "fig2_error_mix_spider_eval.png")
    fig.savefig(out_path, dpi=150)
    print(f"wrote {out_path}")


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    make_fig1()
    make_fig2()


if __name__ == "__main__":
    main()
