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
