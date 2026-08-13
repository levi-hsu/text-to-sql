"""Calibrate check_sft_checkpoint.py's schema-hallucination check against
gold SQL, to set --max-hallucination-rate from data instead of a guess.

check_schema_hallucination() in check_sft_checkpoint.py is a heuristic,
regex-based check, not a full SQL parser (see its NOTE for the specific
gaps: unquoted identifiers are invisible to it, and unresolved aliases --
CTEs, subquery aliases -- fall back to a lenient whole-schema check). So it
will flag a small fraction of *correct* queries as hallucinating, even
though every gold query is correct by construction. Running it against
dev_gold.sql instead of model predictions measures exactly that: whatever
nonzero rate comes back is the checker's own noise floor, not a real
hallucination.

That floor is the right basis for --max-hallucination-rate: requiring a
model's rate below the floor is requiring something the checker itself
cannot certify even for ground truth, and a threshold picked without this
number is arbitrary. Compare against a second run on
runs/baseline_qwen2.5coder3b/spider_dev_raw.jsonl (the untrained zero-shot
arm) to see the other end of the range: how far a model with no schema
grounding at all sits above the floor.

Usage:
  python scripts/calibrate_hallucination_floor.py \
      --gold data/spider_data/dev_gold.sql \
      --tables data/spider_data/tables.json

  # for comparison, the same check against baseline model predictions:
  python scripts/calibrate_hallucination_floor.py \
      --gold data/spider_data/dev_gold.sql \
      --tables data/spider_data/tables.json \
      --raw runs/baseline_qwen2.5coder3b/spider_dev_raw.jsonl
"""

import argparse

from check_sft_checkpoint import check_schema_hallucination
from data_utils import load_gold_sql
from schema_utils import load_tables


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--gold", required=True, help="*_gold.sql file (used unless --raw is given)")
    parser.add_argument("--tables", required=True, help="tables.json for the same dataset")
    parser.add_argument(
        "--raw", default=None,
        help="Optional *_raw.jsonl from generate_sql.py -- if given, checks these "
        "predictions instead of gold SQL, for comparison against the floor.",
    )
    args = parser.parse_args()

    tables_by_db = load_tables(args.tables)

    if args.raw:
        import json
        with open(args.raw, "r") as f:
            records = [json.loads(line) for line in f if line.strip()]
        label = f"predictions ({args.raw})"
    else:
        pairs = load_gold_sql(args.gold)
        records = [{"db_id": db_id, "pred_sql": sql} for sql, db_id in pairs]
        label = f"gold SQL ({args.gold})"

    result = check_schema_hallucination(records, tables_by_db)

    print(f"Hallucination rate on {label}: {result['rate']:.4f} "
          f"({result['count']}/{result['total']})")
    if result["examples"]:
        tag = "false positives (checker limitation)" if not args.raw else "flagged cases"
        print(f"Sample {tag}:")
        for ex in result["examples"][:5]:
            print(f"  index={ex['index']} db_id={ex['db_id']} bogus={ex['bogus_identifiers']}")


if __name__ == "__main__":
    main()
