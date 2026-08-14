import os
from collections import Counter

from eval_sql import execute_query, get_connection, rows_match
from sql_extract import extract_sql

_missing_db_warned = set()


def _row_overlap_fraction(pred_rows, gold_rows) -> float:
    """Fraction of gold's rows also present in pred (multiset intersection,
    same string-repr fallback rows_match uses for unorderable/mixed types).
    gold_rows is never empty here in practice -- an empty gold_rows with an
    empty pred_rows would already have scored as a match upstream -- but
    handled defensively regardless.
    """
    if not gold_rows:
        return 0.0
    pred_counter = Counter(map(str, pred_rows))
    gold_counter = Counter(map(str, gold_rows))
    overlap = sum((pred_counter & gold_counter).values())
    return overlap / len(gold_rows)


def make_execution_reward(
    db_dir: str,
    timeout_sec: int = 10,
    partial_credit: float = 0.0,
    partial_credit_mode: str = "flat",
    graduated_floor: float = 0.02,
    graduated_max: float = 0.3,
):
    """Build a reward_fn matching TRL's GRPOTrainer signature:
    reward_fn(prompts, completions, **kwargs) -> list[float], where extra
    training-dataset columns (db_id, gold_sql here) are forwarded as kwargs
    automatically by TRL, matching column names.

    A single sqlite3 connection cache is closed over by the returned
    function and persists across calls for the life of the training run,
    the same caching pattern eval_sql.py uses within one script invocation.

    partial_credit: reward for a wrong-but-executing completion in "flat"
    mode (see module docstring). Must be in [0, 1); 0.0 (the default)
    reproduces the original strict binary reward. Ignored in "graduated" mode.
    partial_credit_mode: "flat" (default, original behavior) or "graduated"
    (row-overlap scaled between graduated_floor and graduated_max).
    """
    if partial_credit_mode not in ("flat", "graduated"):
        raise ValueError(f"partial_credit_mode must be 'flat' or 'graduated', got {partial_credit_mode!r}")
    if not (0.0 <= partial_credit < 1.0):
        raise ValueError(f"partial_credit must be in [0, 1), got {partial_credit}")
    if not (0.0 <= graduated_floor <= graduated_max < 1.0):
        raise ValueError(
            f"graduated_floor/graduated_max must satisfy 0 <= floor <= max < 1, "
            f"got floor={graduated_floor}, max={graduated_max}"
        )

    conn_cache: dict = {}

    def reward_fn(prompts, completions, db_id, gold_sql, **kwargs):
        rewards = []
        for completion, db, gold in zip(completions, db_id, gold_sql):
            # TRL's conversational mode passes completions as message lists
            # ([{"role": "assistant", "content": "..."}]); non-conversational
            # mode passes plain strings. Handle both without assuming which
            # this TRL version/config uses -- not verified against a live
            # TRL install (see train_rl.py's top-level NOTE).
            raw_text = completion if isinstance(completion, str) else completion[-1]["content"]
            pred_sql = extract_sql(raw_text)

            db_path = os.path.join(db_dir, db, f"{db}.sqlite")
            if not os.path.exists(db_path):
                if db not in _missing_db_warned:
                    print(f"[rl_reward] WARNING: no sqlite file for db_id '{db}' at {db_path}; "
                          "scoring every example using it as 0.0 rather than crashing training.")
                    _missing_db_warned.add(db)
                rewards.append(0.0)
                continue

            conn = get_connection(db_dir, db, conn_cache)
            order_matters = "order by" in gold.lower()

            gold_rows, gold_err = execute_query(conn, gold, timeout_sec)
            if gold_err is not None:
                # Data problem (bad gold row), not a model problem -- same
                # convention as eval_sql.py: never reward or penalize the
                # model for a gold query that doesn't itself execute.
                rewards.append(0.0)
                continue

            pred_rows, pred_err = execute_query(conn, pred_sql, timeout_sec)
            match = pred_err is None and rows_match(gold_rows, pred_rows, order_matters)
            if match:
                rewards.append(1.0)
            elif pred_err is None:
                # Executed cleanly (so it referenced real tables/columns and
                # was syntactically valid -- a hallucinated identifier or
                # malformed query would have raised pred_err instead), just
                # returned the wrong rows.
                if partial_credit_mode == "graduated":
                    overlap = _row_overlap_fraction(pred_rows, gold_rows)
                    rewards.append(graduated_floor + (graduated_max - graduated_floor) * overlap)
                else:
                    rewards.append(partial_credit)
            else:
                rewards.append(0.0)
        return rewards

    return reward_fn
