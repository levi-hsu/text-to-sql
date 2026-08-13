"""Error taxonomy: categorize each arm's *incorrect* predictions by failure
mode (plan.md, "Error taxonomy" -- named as the project's main original
contribution over the papers it scales down from, which report aggregate
execution accuracy but not this breakdown).

Needs no new training or generation -- it reads the *_results.json files
eval_sql.py already wrote for every arm (baseline/SFT/RL/RL-v2, on both
Spider-dev and BIRD-dev), each of which already carries gold_sql, pred_sql,
pred_error, and match per example.

Categories, and how each is detected:

  For predictions that fail to execute at all (pred_error is not None):
  this reuses SQLite's OWN error text as ground truth, rather than writing
  a bespoke identifier-parsing regex (the project already found that
  approach brittle: check_sft_checkpoint.py's schema_hallucination check is
  blind to this model's bare, unquoted identifier style -- see plan.md's
  note on that). SQLite raises a distinct, literal error message for "no
  such table: X" and "no such column: X" regardless of quoting convention,
  so these are read directly off execute_query's own error string:
    - schema_table_error   : "no such table"
    - schema_column_error  : "no such column" or "ambiguous column name"
    - syntax_error         : "syntax error", "incomplete input", "malformed",
                              "unrecognized token"
    - timeout               : pred_error == "timeout" (eval_sql.py's own convention)
    - other_execution_error : any other execution failure (e.g. misused
                              aggregate, datatype mismatch, wrong argument count)

  For predictions that execute but return the wrong result (pred_error is
  None, match is False): there is no error message to read, so these are
  structural, keyword-level comparisons against the gold query -- heuristic,
  not a parser, and explicitly a relative (across-arm) signal rather than a
  claim of precise per-example ground truth, same spirit as
  check_sft_checkpoint.py's own documented heuristics:
    - join_structure_mismatch : JOIN keyword count differs from gold
    - subquery_structure_mismatch : nested-SELECT count, or presence of
                              INTERSECT/EXCEPT/UNION, differs from gold
    - aggregation_mismatch   : set of aggregate functions used (count/sum/
                              avg/min/max), or GROUP BY/HAVING presence,
                              differs from gold
    - orderby_limit_mismatch : ORDER BY or LIMIT presence differs from gold
    - table_reference_mismatch : the set of table names following FROM/JOIN
                              differs from gold (misses subquery-derived
                              FROMs by construction -- only literal table
                              tokens are matched, a known, documented gap)
    - other_wrong_result     : none of the above fired; likely a wrong
                              constant, wrong column, or wrong WHERE
                              condition that this heuristic set can't see

A prediction can get multiple tags (a query can have both a join problem
and an aggregation problem). primary_category picks the first match in a
fixed priority order (execution-failure categories first, since those are
directly observed rather than guessed; then structural categories) purely
so there is also a single mutually-exclusive breakdown that sums to 100% of
incorrect predictions, alongside the multi-label rates.

Usage:
  python scripts/error_taxonomy.py \
      --results runs/sft_qwen2.5coder3b_eval/spider_dev_results.json \
      --output runs/sft_qwen2.5coder3b_eval/spider_dev_taxonomy.json
"""

import argparse
import json
import re
from collections import Counter
from typing import Dict, List, Optional

SYNTAX_ERROR_PATTERNS = (
    "syntax error",
    "incomplete input",
    "malformed",
    "unrecognized token",
)
SCHEMA_TABLE_PATTERNS = ("no such table",)
SCHEMA_COLUMN_PATTERNS = ("no such column", "ambiguous column name")

JOIN_RE = re.compile(r"\bjoin\b", re.IGNORECASE)
SELECT_RE = re.compile(r"\bselect\b", re.IGNORECASE)
SET_OP_RE = re.compile(r"\b(intersect|except|union)\b", re.IGNORECASE)
AGG_FN_RE = re.compile(r"\b(count|sum|avg|min|max)\s*\(", re.IGNORECASE)
GROUP_BY_RE = re.compile(r"\bgroup\s+by\b", re.IGNORECASE)
HAVING_RE = re.compile(r"\bhaving\b", re.IGNORECASE)
ORDER_BY_RE = re.compile(r"\border\s+by\b", re.IGNORECASE)
LIMIT_RE = re.compile(r"\blimit\b", re.IGNORECASE)
FROM_JOIN_TABLE_RE = re.compile(r"\b(?:from|join)\s+([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)

# Priority order for primary_category (see module docstring). Execution
# failures are directly observed (from SQLite's own error), so they take
# priority over the structural guesses used for wrong-but-executing rows.
CATEGORY_PRIORITY = [
    "syntax_error",
    "schema_table_error",
    "schema_column_error",
    "timeout",
    "other_execution_error",
    "table_reference_mismatch",
    "join_structure_mismatch",
    "subquery_structure_mismatch",
    "aggregation_mismatch",
    "orderby_limit_mismatch",
    "other_wrong_result",
]


def categorize_execution_error(pred_error: str) -> List[str]:
    err_lower = pred_error.lower()
    if pred_error == "timeout":
        return ["timeout"]
    if any(p in err_lower for p in SCHEMA_TABLE_PATTERNS):
        return ["schema_table_error"]
    if any(p in err_lower for p in SCHEMA_COLUMN_PATTERNS):
        return ["schema_column_error"]
    if any(p in err_lower for p in SYNTAX_ERROR_PATTERNS):
        return ["syntax_error"]
    return ["other_execution_error"]


def _referenced_tables(sql: str) -> set:
    return {t.lower() for t in FROM_JOIN_TABLE_RE.findall(sql)}


def categorize_wrong_result(pred_sql: str, gold_sql: str) -> List[str]:
    tags = []

    if len(JOIN_RE.findall(pred_sql)) != len(JOIN_RE.findall(gold_sql)):
        tags.append("join_structure_mismatch")

    pred_subq = len(SELECT_RE.findall(pred_sql)) - 1
    gold_subq = len(SELECT_RE.findall(gold_sql)) - 1
    pred_setop = bool(SET_OP_RE.search(pred_sql))
    gold_setop = bool(SET_OP_RE.search(gold_sql))
    if pred_subq != gold_subq or pred_setop != gold_setop:
        tags.append("subquery_structure_mismatch")

    pred_aggs = {m.lower() for m in AGG_FN_RE.findall(pred_sql)}
    gold_aggs = {m.lower() for m in AGG_FN_RE.findall(gold_sql)}
    pred_group = bool(GROUP_BY_RE.search(pred_sql))
    gold_group = bool(GROUP_BY_RE.search(gold_sql))
    pred_having = bool(HAVING_RE.search(pred_sql))
    gold_having = bool(HAVING_RE.search(gold_sql))
    if pred_aggs != gold_aggs or pred_group != gold_group or pred_having != gold_having:
        tags.append("aggregation_mismatch")

    pred_order = bool(ORDER_BY_RE.search(pred_sql))
    gold_order = bool(ORDER_BY_RE.search(gold_sql))
    pred_limit = bool(LIMIT_RE.search(pred_sql))
    gold_limit = bool(LIMIT_RE.search(gold_sql))
    if pred_order != gold_order or pred_limit != gold_limit:
        tags.append("orderby_limit_mismatch")

    if _referenced_tables(pred_sql) != _referenced_tables(gold_sql):
        tags.append("table_reference_mismatch")

    if not tags:
        tags.append("other_wrong_result")

    return tags


def categorize(pred_sql: str, gold_sql: str, pred_error: Optional[str]) -> List[str]:
    if pred_error is not None:
        return categorize_execution_error(pred_error)
    return categorize_wrong_result(pred_sql, gold_sql)


def primary_category(tags: List[str]) -> str:
    for cat in CATEGORY_PRIORITY:
        if cat in tags:
            return cat
    return tags[0]  # should not happen given CATEGORY_PRIORITY covers every tag categorize() emits


def build_report(results: List[dict]) -> dict:
    incorrect = [r for r in results if r.get("gold_error") is None and not r["match"]]
    total = len(results)
    n_incorrect = len(incorrect)

    multi_label_counts = Counter()
    primary_counts = Counter()
    examples_by_category: Dict[str, list] = {}

    for r in incorrect:
        tags = categorize(r["pred_sql"], r["gold_sql"], r.get("pred_error"))
        for t in tags:
            multi_label_counts[t] += 1
            bucket = examples_by_category.setdefault(t, [])
            if len(bucket) < 5:
                bucket.append(
                    {"index": r["index"], "db_id": r["db_id"], "gold_sql": r["gold_sql"],
                     "pred_sql": r["pred_sql"], "pred_error": r.get("pred_error")}
                )
        primary_counts[primary_category(tags)] += 1

    def rate_table(counts: Counter, denom: int) -> dict:
        return {
            cat: {"count": counts.get(cat, 0), "rate_of_incorrect": counts.get(cat, 0) / denom if denom else 0.0}
            for cat in CATEGORY_PRIORITY
        }

    return {
        "total_examples": total,
        "n_correct": total - n_incorrect,
        "n_incorrect": n_incorrect,
        "multi_label": rate_table(multi_label_counts, n_incorrect),
        "primary_category": rate_table(primary_counts, n_incorrect),
        "examples": examples_by_category,
    }


def print_report(report: dict, label: str):
    print(f"\n{label}: {report['n_incorrect']} incorrect / {report['total_examples']} total")
    print(f"{'category':<28}{'count':>8}{'rate_of_incorrect':>20}")
    for cat in CATEGORY_PRIORITY:
        m = report["multi_label"][cat]
        print(f"  {cat:<26}{m['count']:>8}{m['rate_of_incorrect']:>20.4f}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--results", required=True, help="*_results.json from eval_sql.py")
    parser.add_argument("--output", default=None, help="Where to write the JSON report")
    parser.add_argument("--label", default=None, help="Label for the printed summary (defaults to --results path)")
    args = parser.parse_args()

    with open(args.results) as f:
        results = json.load(f)["results"]

    report = build_report(results)
    print_report(report, args.label or args.results)

    if args.output:
        with open(args.output, "w") as f:
            json.dump(report, f, indent=2)
        print(f"\nWrote full report to {args.output}")


if __name__ == "__main__":
    main()
