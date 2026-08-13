"""Build the held-out slice scripts/rl_health_callback.py generates against
during RL training (configs/rl.yaml's monitor.heldout_jsonl, an open item
until this script existed). Drawn from Spider-dev, in the same
{db_id, question, schema, gold_sql} shape build_sft_data.py already uses.

No train/monitor leakage: the RL arm trains on data/sft/spider_train_subset.jsonl
(Spider-train), entirely disjoint from Spider-dev by construction of the
Spider split -- this is the same dev set generate_sql.py/eval_sql.py already
use for the SFT and baseline arms' offline evaluation, just re-purposed here
for a cheap in-training health check, not a new held-out source.

Usage:
  python scripts/build_rl_heldout.py --size 50 --output data/sft/rl_heldout.jsonl
"""

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
