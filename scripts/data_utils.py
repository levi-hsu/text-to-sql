"""Shared I/O helpers for the baseline generate/eval scripts."""

import json
import os
from typing import Dict, List, Optional, Tuple

import yaml


def load_config(config_path: str) -> dict:
    with open(config_path, "r") as f:
        return yaml.safe_load(f)


def load_examples(dev_json_path: str) -> List[dict]:
    """Load a Spider/BIRD-format *.json split file.

    Each example is a dict with at least "question" and "db_id" keys, which
    is all generate_sql.py needs. Order is preserved -- eval_sql.py assumes
    predictions are written in this same order, one per line, matching the
    line order of the corresponding *_gold.sql file (this is the convention
    Spider's own evaluation scripts use).
    """
    with open(dev_json_path, "r") as f:
        examples = json.load(f)
    for ex in examples:
        if "question" not in ex or "db_id" not in ex:
            raise ValueError(f"Example missing 'question' or 'db_id': {ex}")
    return examples


def load_gold_sql(gold_sql_path: str) -> List[Tuple[str, str]]:
    """Load a *_gold.sql file: one 'SQL<TAB>db_id' pair per line."""
    pairs = []
    with open(gold_sql_path, "r") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line.strip():
                continue
            sql, db_id = line.split("\t")
            pairs.append((sql.strip(), db_id.strip()))
    return pairs


def resolve_dataset_paths(config: dict, dataset: str) -> Optional[Dict[str, str]]:
    """Pull the {tables, db_dir, dev, dev_gold} paths for 'spider' or 'bird'.

    Returns None (with a warning printed) if the dataset's paths are not
    configured yet -- this is expected for 'bird' until BIRD-dev is
    downloaded (see plan.md, "Open items").
    """
    data_cfg = config["data"]
    keys = {
        "tables": f"{dataset}_tables",
        "db_dir": f"{dataset}_db_dir",
        "dev": f"{dataset}_dev",
        "dev_gold": f"{dataset}_dev_gold",
    }
    paths = {name: data_cfg.get(key) for name, key in keys.items()}
    if any(v is None for v in paths.values()):
        print(
            f"[data_utils] '{dataset}' dataset paths are not fully configured in "
            f"the config file (got {paths}). Skipping."
        )
        return None
    for name, path in paths.items():
        if not os.path.exists(path):
            raise FileNotFoundError(f"{dataset}.{name} points to a missing path: {path}")
    return paths
