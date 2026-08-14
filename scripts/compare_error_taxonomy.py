import json
import os

from error_taxonomy import CATEGORY_PRIORITY, build_report

models = [
    ("baseline", "runs/baseline_qwen2.5coder3b/spider_dev_results.json", "runs/baseline_qwen2.5coder3b/bird_dev_results.json"),
    ("sft", "runs/sft_qwen2.5coder3b_eval/spider_dev_results.json", "runs/sft_qwen2.5coder3b_eval/bird_dev_results.json"),
    ("rl", "runs/rl_qwen2.5coder3b_eval/spider_dev_results.json", "runs/rl_qwen2.5coder3b_eval/bird_dev_results.json"),
    ("rl_v2", "runs/rl_qwen2.5coder3b_v2_eval/spider_dev_results.json", "runs/rl_qwen2.5coder3b_v2_eval/bird_dev_results.json"),
]


def load_and_report(path):
    if not os.path.exists(path):
        return None
    with open(path) as f:
        results = json.load(f)["results"]
    report = build_report(results)
    out_path = path.replace("_results.json", "_taxonomy.json")
    with open(out_path, "w") as f:
        json.dump(report, f, indent=2)
    return report


def print_table(reports_by_arm: dict, dataset_label: str):
    print(f"\n=== {dataset_label}: category rate of incorrect predictions, by arm ===")
    header = f"{'category':<28}" + "".join(f"{name:>12}" for name, _ in reports_by_arm.items())
    print(header)
    print("-" * len(header))
    for cat in CATEGORY_PRIORITY:
        row = f"{cat:<28}"
        for name, report in reports_by_arm.items():
            if report is None:
                row += f"{'--':>12}"
            else:
                row += f"{report['multi_label'][cat]['rate_of_incorrect']:>12.4f}"
        print(row)

    print(f"\n{'n_incorrect / total':<28}", end="")
    for name, report in reports_by_arm.items():
        if report is None:
            print(f"{'--':>12}", end="")
        else:
            frac_str = f"{report['n_incorrect']}/{report['total_examples']}"
            print(f"{frac_str:>12}", end="")
    print()


def main():
    spider_reports = {}
    bird_reports = {}
    for name, spider_path, bird_path in models:
        spider_reports[name] = load_and_report(spider_path)
        bird_reports[name] = load_and_report(bird_path)

    print_table(spider_reports, "Spider eval (in-distribution)")
    print_table(bird_reports, "BIRD eval (out-of-distribution)")

    print(
        "\nNote: multi-label rates -- a single incorrect prediction can be tagged with "
        "multiple categories, so columns do not sum to 1.0. See each arm's "
        "*_taxonomy.json (written next to the source *_results.json) for the "
        "mutually-exclusive primary_category breakdown, per-category example indices, "
        "and error_taxonomy.py's module docstring for exactly how each category is "
        "detected and its known heuristic gaps."
    )


if __name__ == "__main__":
    main()
