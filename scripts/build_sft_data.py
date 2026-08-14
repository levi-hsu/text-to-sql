import argparse
import json
import os
import random

from transformers import AutoTokenizer

from data_utils import load_config
from prompt_template import build_messages
from schema_utils import get_schema_str, load_tables


def load_spider_examples(path: str) -> list:
    with open(path, "r") as f:
        return json.load(f)


def build_full_messages(question: str, schema_str: str, gold_sql: str) -> list:
    """Same (system, user) turns generate_sql.py uses, plus the gold assistant turn."""
    messages = build_messages(question, schema_str)
    messages.append({"role": "assistant", "content": gold_sql})
    return messages


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="configs/sft.yaml")
    args = parser.parse_args()

    config = load_config(args.config)
    data_cfg = config["data"]

    tables_by_db = load_tables(data_cfg["spider_tables"])

    raw_examples = load_spider_examples(data_cfg["train_spider"])
    print(f"[build_sft_data] Loaded {len(raw_examples)} examples from {data_cfg['train_spider']}")
    if data_cfg.get("use_train_others", False):
        others = load_spider_examples(data_cfg["train_others"])
        print(f"[build_sft_data] Loaded {len(others)} examples from {data_cfg['train_others']}")
        raw_examples = raw_examples + others

    print(f"[build_sft_data] Loading tokenizer from {config['model']['name_or_path']} ...")
    tokenizer = AutoTokenizer.from_pretrained(config["model"]["name_or_path"])

    max_seq_length = data_cfg["max_seq_length"]
    kept = []
    n_missing_db = 0
    n_empty_sql = 0
    n_too_long = 0

    for ex in raw_examples:
        db_id = ex["db_id"]
        gold_sql = ex.get("query", "").strip()
        question = ex.get("question", "").strip()

        if db_id not in tables_by_db:
            n_missing_db += 1
            continue
        if not gold_sql:
            n_empty_sql += 1
            continue

        schema_str = get_schema_str(db_id, tables_by_db)
        if not gold_sql.endswith(";"):
            gold_sql = gold_sql + ";"

        full_messages = build_full_messages(question, schema_str, gold_sql)
        # See train_sft.py for why this goes through text instead of
        # apply_chat_template(tokenize=True) directly (version-dependent
        # return type across transformers releases).
        full_text = tokenizer.apply_chat_template(
            full_messages, tokenize=False, add_generation_prompt=False
        )
        full_ids = tokenizer(full_text, add_special_tokens=False)["input_ids"]
        if len(full_ids) > max_seq_length:
            n_too_long += 1
            continue

        kept.append(
            {
                "db_id": db_id,
                "question": question,
                "schema": schema_str,
                "gold_sql": gold_sql,
            }
        )

    print(
        f"[build_sft_data] Filtered out: {n_missing_db} missing db_id, "
        f"{n_empty_sql} empty gold SQL, {n_too_long} over max_seq_length={max_seq_length}"
    )
    print(f"[build_sft_data] {len(kept)} examples passed filtering")

    seed = data_cfg.get("seed", 42)
    rng = random.Random(seed)
    rng.shuffle(kept)

    subset_size = data_cfg.get("subset_size")
    if subset_size is not None and subset_size < len(kept):
        kept = kept[:subset_size]
    print(f"[build_sft_data] Keeping {len(kept)} examples after subset_size={subset_size} (seed={seed})")

    out_path = data_cfg["sft_data_out"]
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w") as f:
        for row in kept:
            f.write(json.dumps(row) + "\n")

    print(f"[build_sft_data] Wrote {len(kept)} training examples to {out_path}")


if __name__ == "__main__":
    main()
