"""
Build SFT and evaluation JSONL files from the raw Spider download.

Reads from <project_root>/spider_data/ (tables.json, train_spider.json,
train_others.json, dev.json -- the standard Spider release layout) and
writes to <project_root>/data/:

  data/sft_diverse/train.jsonl   -- schema-agnostic SFT warm-start (PLAN.md
                                     Stage 1, many-database condition), chat
                                     format, one example per line.
  data/sft_diverse/valid.jsonl   -- small in-domain slice held out from the
                                     same pool, for training-loss monitoring
                                     only (NOT the generalization eval).
  data/sft_narrow/train.jsonl    -- schema-specific SFT warm-start (Stage 1,
                                     single-database condition), all from one
                                     fixed db_id.
  data/sft_narrow/valid.jsonl    -- small in-domain slice for loss monitoring.
  data/eval/narrow_heldout.jsonl -- held-out questions on the SAME db_id as
                                     sft_narrow, for in-domain eval (PLAN.md
                                     Experiment B logic). Not used in training.
  data/eval/dev.jsonl            -- Spider's official dev split (disjoint
                                     databases from all training data above),
                                     for cross-schema generalization eval and
                                     for the baseline zero-shot eval. Untouched
                                     Spider dev.json, just reformatted.

Every eval/*.jsonl record carries enough information (db_id, gold SQL,
schema DDL, sqlite db path relative to spider_data/database/) for eval_sql.py
to run execution accuracy directly, and for run_baseline_eval.py /
mlx_lm_lora.train to build the exact same prompt used at SFT time.

Usage:
    python3 scripts/prepare_spider_data.py
"""

from __future__ import annotations

import json
import random
from pathlib import Path

from sql_prompt import build_schema_ddl, build_chat_messages, build_user_message

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SPIDER_DIR = PROJECT_ROOT / "spider_data"
DATA_DIR = PROJECT_ROOT / "data"

# Fixed choice for the schema-specific (narrow) condition: the training
# database with the most question-SQL pairs in train_spider.json, so the
# in-domain train/held-out split has the most statistical power available.
NARROW_DB_ID = "college_2"

RANDOM_SEED = 0
DIVERSE_VALID_FRACTION = 0.04   # ~350 of 8659, for loss monitoring only
NARROW_HELDOUT_FRACTION = 0.30  # in-domain generalization-within-schema eval
NARROW_VALID_FRACTION = 0.10    # of the narrow *train* split, for loss monitoring


def load_json(path: Path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_jsonl(path: Path, records: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def main():
    tables = load_json(SPIDER_DIR / "tables.json")
    schema_by_db = {t["db_id"]: t for t in tables}
    ddl_by_db = {db_id: build_schema_ddl(entry) for db_id, entry in schema_by_db.items()}

    train_spider = load_json(SPIDER_DIR / "train_spider.json")
    train_others = load_json(SPIDER_DIR / "train_others.json")
    dev = load_json(SPIDER_DIR / "dev.json")

    diverse_pool = train_spider + train_others
    db_ids_diverse = sorted({ex["db_id"] for ex in diverse_pool})
    print(f"Diverse (schema-agnostic) pool: {len(diverse_pool)} examples across {len(db_ids_diverse)} databases")

    if NARROW_DB_ID not in schema_by_db:
        raise SystemExit(f"NARROW_DB_ID={NARROW_DB_ID!r} not found in tables.json")
    narrow_pool = [ex for ex in diverse_pool if ex["db_id"] == NARROW_DB_ID]
    print(f"Narrow (schema-specific) pool: {len(narrow_pool)} examples, db_id={NARROW_DB_ID}")
    if len(narrow_pool) < 30:
        raise SystemExit(
            f"NARROW_DB_ID={NARROW_DB_ID!r} only has {len(narrow_pool)} examples; "
            "pick a db_id with more training examples (see script docstring)."
        )

    rng = random.Random(RANDOM_SEED)

    # ---- Diverse (schema-agnostic) SFT split ----
    diverse = list(diverse_pool)
    rng.shuffle(diverse)
    n_valid = max(1, int(len(diverse) * DIVERSE_VALID_FRACTION))
    diverse_valid_ex, diverse_train_ex = diverse[:n_valid], diverse[n_valid:]

    def to_sft_record(ex: dict) -> dict:
        db_id = ex["db_id"]
        ddl = ddl_by_db[db_id]
        return {"messages": build_chat_messages(ddl, ex["question"], ex["query"])}

    write_jsonl(DATA_DIR / "sft_diverse" / "train.jsonl", [to_sft_record(e) for e in diverse_train_ex])
    write_jsonl(DATA_DIR / "sft_diverse" / "valid.jsonl", [to_sft_record(e) for e in diverse_valid_ex])
    print(f"Wrote sft_diverse: {len(diverse_train_ex)} train / {len(diverse_valid_ex)} valid")

    # ---- Narrow (schema-specific) SFT split ----
    narrow = list(narrow_pool)
    rng.shuffle(narrow)
    n_heldout = max(1, int(len(narrow) * NARROW_HELDOUT_FRACTION))
    narrow_heldout_ex = narrow[:n_heldout]
    narrow_trainpool_ex = narrow[n_heldout:]
    n_valid = max(1, int(len(narrow_trainpool_ex) * NARROW_VALID_FRACTION))
    narrow_valid_ex, narrow_train_ex = narrow_trainpool_ex[:n_valid], narrow_trainpool_ex[n_valid:]

    write_jsonl(DATA_DIR / "sft_narrow" / "train.jsonl", [to_sft_record(e) for e in narrow_train_ex])
    write_jsonl(DATA_DIR / "sft_narrow" / "valid.jsonl", [to_sft_record(e) for e in narrow_valid_ex])
    print(f"Wrote sft_narrow: {len(narrow_train_ex)} train / {len(narrow_valid_ex)} valid")

    def to_eval_record(ex: dict) -> dict:
        db_id = ex["db_id"]
        ddl = ddl_by_db[db_id]
        return {
            "db_id": db_id,
            "db_path": f"database/{db_id}/{db_id}.sqlite",  # relative to spider_data/
            "question": ex["question"],
            "gold_sql": ex["query"],
            "schema_ddl": ddl,
            "user_message": build_user_message(ddl, ex["question"]),
        }

    write_jsonl(DATA_DIR / "eval" / "narrow_heldout.jsonl", [to_eval_record(e) for e in narrow_heldout_ex])
    print(f"Wrote eval/narrow_heldout.jsonl: {len(narrow_heldout_ex)} examples (db_id={NARROW_DB_ID}, in-domain)")

    write_jsonl(DATA_DIR / "eval" / "dev.jsonl", [to_eval_record(e) for e in dev])
    dev_dbs = sorted({e["db_id"] for e in dev})
    overlap = set(dev_dbs) & set(db_ids_diverse)
    print(f"Wrote eval/dev.jsonl: {len(dev)} examples across {len(dev_dbs)} databases "
          f"(overlap with training databases: {len(overlap)} -- should be 0)")
    if overlap:
        print(f"  WARNING: train/dev database overlap found: {sorted(overlap)}")

    print("\nDone. Data written under:", DATA_DIR)


if __name__ == "__main__":
    main()
