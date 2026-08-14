import argparse
import json

from data_utils import load_examples, load_gold_sql
from schema_utils import get_schema_str, load_tables


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dev", default="data/spider_data/dev.json")
    parser.add_argument("--dev-gold", default="data/spider_data/dev_gold.sql")
    parser.add_argument("--tables", default="data/spider_data/tables.json")
    parser.add_argument("--size", type=int, default=50)
    parser.add_argument("--output", default="data/sft/rl_heldout.jsonl")
    args = parser.parse_args()

    examples = load_examples(args.dev)
    gold_pairs = load_gold_sql(args.dev_gold)
    if len(examples) != len(gold_pairs):
        raise ValueError(
            f"{args.dev} has {len(examples)} examples but {args.dev_gold} has "
            f"{len(gold_pairs)} gold rows -- these must be the same, order-aligned split "
            "(same assumption eval_sql.py makes)."
        )
    tables_by_db = load_tables(args.tables)

    rows = []
    for ex, (gold_sql, gold_db_id) in list(zip(examples, gold_pairs))[: args.size]:
        if ex["db_id"] != gold_db_id:
            raise ValueError(f"db_id mismatch at this row: dev.json has '{ex['db_id']}', "
                              f"dev_gold.sql has '{gold_db_id}' -- files are not aligned.")
        rows.append({
            "db_id": ex["db_id"],
            "question": ex["question"],
            "schema": get_schema_str(ex["db_id"], tables_by_db),
            "gold_sql": gold_sql,
        })

    with open(args.output, "w") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")
    print(f"[build_rl_heldout] Wrote {len(rows)} held-out examples to {args.output}")


if __name__ == "__main__":
    main()
