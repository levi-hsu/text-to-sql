"""Build the data splits for Experiment 2: after SFT on Spider, continue
training (SFT and RL, separately) on a SMALL slice of BIRD-dev, and see
whether either recovers BIRD generalization -- see the plan.md-adjacent
discussion this implements. Three-way, db_id-disjoint split of BIRD-dev's
1534 examples, all deterministic given --seed:

  pool databases (--pool-dbs, default debit_card_specializing + california_schools,
  64 + 89 = 153 examples, ~10% of BIRD-dev -- the two smallest databases,
  keeping this a genuinely small slice): split again 80/20 into
    - train rows   (~122): the only rows either continuation arm actually
      trains on.
    - pool-heldout rows (~31): same two schemas, different questions, NEVER
      trained on. This is the memorization check -- if a checkpoint does
      well here but not on crossdb-eval below, it learned these two
      databases' surface patterns, not general schema transfer.

  crossdb-eval databases (the other 9): ~1381 examples, entirely untouched
  by training. This is the real schema-transfer test -- unseen databases,
  same as the project's existing BIRD-dev eval, just restricted to the
  subset not used to build the pool above.

Two starting points matter for correctness here, both learned from the
main RL v2 run (see configs/rl.yaml's header): (1) the two continuation
models (SFT-continue, RL-continue) MUST start from the same SFT-on-Spider
checkpoint (runs/sft_qwen2.5coder3b/adapter) so the comparison only varies
the continuation method, not the starting point; (2) the RL continuation
needs num_generations=4, not 2, to actually get gradient signal on this
tiny pool -- see configs/bird_adapt_rl.yaml.

Outputs (all under data.out_dir, default data/bird_adapt/):
  bird_train_pool.jsonl        {question, schema, gold_sql, db_id} -- the
                                 rows both continuation models train on
                                 (train_sft.py / train_rl.py's format)
  bird_pool_heldout.jsonl      same format, for RL's monitor.heldout_jsonl
  bird_pool_heldout_dev.json + bird_pool_heldout_gold.sql
                                 same pool-heldout rows, in the
                                 generate_sql.py/eval_sql.py dev.json+
                                 gold.sql format, for post-hoc eval of any
                                 checkpoint (SFT-continue or RL-continue)
  bird_crossdb_eval_dev.json + bird_crossdb_eval_gold.sql
                                 the crossdb-eval rows, same format -- the
                                 actual generalization-transfer metric

Length filtering mirrors build_sft_data.py exactly (render the same prompt
generate_sql.py/train_sft.py use, tokenize with the target model's own
tokenizer, drop anything over max_seq_length) so a kept row is guaranteed
trainable without silently truncating the gold-SQL label.

Usage (defaults match the pool-db choice discussed and documented above;
override --pool-dbs to try a different split):
  python scripts/build_bird_adapt_data.py
"""

import argparse
import json
import os
import random

from transformers import AutoTokenizer

from prompt_template import build_messages
from schema_utils import get_schema_str, load_tables


def load_bird_examples(path: str) -> list:
    with open(path, "r") as f:
        return json.load(f)


def render_row(ex: dict, tables_by_db: dict) -> dict:
    db_id = ex["db_id"]
    question = ex.get("question", "").strip()
    gold_sql = ex.get("SQL", "").strip()
    if not gold_sql.endswith(";"):
        gold_sql = gold_sql + ";"
    schema_str = get_schema_str(db_id, tables_by_db)
    return {"db_id": db_id, "question": question, "schema": schema_str, "gold_sql": gold_sql}


def fits_length(row: dict, tokenizer, max_seq_length: int) -> bool:
    messages = build_messages(row["question"], row["schema"]) + [
        {"role": "assistant", "content": row["gold_sql"]}
    ]
    text = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=False)
    ids = tokenizer(text, add_special_tokens=False)["input_ids"]
    return len(ids) <= max_seq_length


def write_jsonl(rows: list, path: str):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps({"db_id": r["db_id"], "question": r["question"],
                                 "schema": r["schema"], "gold_sql": r["gold_sql"]}) + "\n")


def write_dev_and_gold(rows: list, examples_by_key: dict, dev_path: str, gold_path: str):
    """rows have {db_id, question, schema, gold_sql}; examples_by_key maps
    (db_id, question) -> the original BIRD dev.json example, so dev_path can
    carry the original question_id/evidence/difficulty fields too (not
    required by generate_sql.py, but useful for anyone inspecting this
    later) while gold_path matches eval_sql.py's expected SQL<TAB>db_id format.
    """
    os.makedirs(os.path.dirname(dev_path) or ".", exist_ok=True)
    dev_examples = []
    gold_lines = []
    for r in rows:
        orig = examples_by_key[(r["db_id"], r["question"])]
        dev_examples.append(orig)
        gold_sql_no_semi = r["gold_sql"].rstrip(";").strip()
        gold_lines.append(f"{gold_sql_no_semi}\t{r['db_id']}")
    with open(dev_path, "w") as f:
        json.dump(dev_examples, f, indent=2)
    with open(gold_path, "w") as f:
        f.write("\n".join(gold_lines) + "\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bird-dev", default="data/bird-dev/dev.json")
    parser.add_argument("--bird-tables", default="data/bird-dev/dev_tables.json")
    parser.add_argument("--model", default="/home/levi/Documents/llm/Qwen2.5-Coder-3B-Instruct")
    parser.add_argument("--max-seq-length", type=int, default=1024)
    parser.add_argument(
        "--pool-dbs", nargs="+", default=["debit_card_specializing", "california_schools"],
        help="db_ids that form the small training pool (default: the two smallest BIRD-dev dbs, ~153 examples total)",
    )
    parser.add_argument("--pool-heldout-frac", type=float, default=0.2,
                         help="Fraction of the pool held out from training, for the memorization check")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out-dir", default="data/bird_adapt")
    args = parser.parse_args()

    tables_by_db = load_tables(args.bird_tables)
    raw_examples = load_bird_examples(args.bird_dev)
    print(f"[build_bird_adapt_data] Loaded {len(raw_examples)} BIRD-dev examples")

    pool_set = set(args.pool_dbs)
    all_db_ids = sorted({ex["db_id"] for ex in raw_examples})
    crossdb_set = set(all_db_ids) - pool_set
    missing = pool_set - set(all_db_ids)
    if missing:
        raise ValueError(f"--pool-dbs {sorted(missing)} not found in {args.bird_dev} (available: {all_db_ids})")
    print(f"[build_bird_adapt_data] Pool dbs: {sorted(pool_set)} | crossdb-eval dbs: {sorted(crossdb_set)}")

    print(f"[build_bird_adapt_data] Loading tokenizer from {args.model} ...")
    tokenizer = AutoTokenizer.from_pretrained(args.model)

    pool_examples, crossdb_examples = [], []
    examples_by_key = {}
    n_too_long = 0
    for ex in raw_examples:
        row = render_row(ex, tables_by_db)
        examples_by_key[(row["db_id"], row["question"])] = ex
        if not fits_length(row, tokenizer, args.max_seq_length):
            n_too_long += 1
            continue
        if ex["db_id"] in pool_set:
            pool_examples.append(row)
        else:
            crossdb_examples.append(row)
    print(f"[build_bird_adapt_data] Dropped {n_too_long} examples over max_seq_length={args.max_seq_length}")
    print(f"[build_bird_adapt_data] Pool: {len(pool_examples)} examples | crossdb-eval: {len(crossdb_examples)} examples")

    rng = random.Random(args.seed)
    rng.shuffle(pool_examples)
    n_heldout = max(1, round(len(pool_examples) * args.pool_heldout_frac))
    pool_heldout = pool_examples[:n_heldout]
    pool_train = pool_examples[n_heldout:]
    print(f"[build_bird_adapt_data] Pool split: {len(pool_train)} train rows, {len(pool_heldout)} pool-heldout rows "
          f"(seed={args.seed}, pool_heldout_frac={args.pool_heldout_frac})")

    out_dir = args.out_dir
    write_jsonl(pool_train, os.path.join(out_dir, "bird_train_pool.jsonl"))
    write_jsonl(pool_heldout, os.path.join(out_dir, "bird_pool_heldout.jsonl"))
    write_dev_and_gold(
        pool_heldout, examples_by_key,
        os.path.join(out_dir, "bird_pool_heldout_dev.json"),
        os.path.join(out_dir, "bird_pool_heldout_gold.sql"),
    )
    write_dev_and_gold(
        crossdb_examples, examples_by_key,
        os.path.join(out_dir, "bird_crossdb_eval_dev.json"),
        os.path.join(out_dir, "bird_crossdb_eval_gold.sql"),
    )

    print(f"[build_bird_adapt_data] Wrote outputs under {out_dir}/:")
    print(f"  bird_train_pool.jsonl          {len(pool_train)} rows (train on these)")
    print(f"  bird_pool_heldout.jsonl        {len(pool_heldout)} rows (never trained on -- memorization check)")
    print(f"  bird_pool_heldout_dev.json/.sql  {len(pool_heldout)} rows (same, generate_sql.py/eval_sql.py format)")
    print(f"  bird_crossdb_eval_dev.json/.sql  {len(crossdb_examples)} rows (schema-disjoint -- the real transfer test)")


if __name__ == "__main__":
    main()
