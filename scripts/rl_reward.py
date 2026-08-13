"""Execution-based reward for the RL arm (see plan.md, "RL arm": "The
primary reward is execution-only -- does the generated SQL run and return
the correct result").

Deliberately built on top of eval_sql.py's own execute_query/rows_match/
get_connection, instead of reimplementing execution comparison here, so
"reward" during training and "execution accuracy" during offline evaluation
are the same computation, not two implementations that could quietly
diverge. This also matters for scripts/rl_health_callback.py's held-out
reward-vs-accuracy check: that check is only meaningful if the reward and
the offline metric are guaranteed identical, not just similar.

Runs against the Spider *train* databases (data/spider_data/database), not
the dev databases -- verified all 140 train_spider db_ids resolve to a
directory there before wiring this up.

partial_credit, partial_credit_mode="flat" (original): the first real RL run
(runs/rl_qwen2.5coder3b) spent essentially the whole 300 steps with
frac_reward_zero_std around 0.65-0.9 -- most sampled groups had zero reward
variance because the binary 0/1 reward only distinguishes "exactly right"
from everything else, and the starting checkpoint was already fairly
decided (right or wrong, consistently) on most training rows. partial_credit
adds a third, small reward value strictly between 0.0 and 1.0 for
completions that execute cleanly but land on the wrong result, so two
samples in a group are less likely to collide on the exact same value
purely by chance. In "flat" mode this does NOT try to measure "how close"
the wrong query was (that kind of partial-correctness reward is exactly
what plan.md's RL arm section already flagged as riskier and
underperforming a simple execution-only reward in Arctic-Text2SQL-R1) --
it's a single flat bonus for "ran without error," which is already fully
determined by execute_query's own error/no-error outcome, no new heuristic
or parser needed. Kept far below the 1.0 correct-match reward (0.1 by
default, see configs/rl.yaml's reward.partial_credit_executes_wrong_result)
specifically so it can never make a wrong-but-executing completion
outscore a correct one within a group -- accuracy stays the dominant,
undiluted optimization target; this only adds gradient signal among
completions that are already wrong.

partial_credit_mode="graduated" (added for the RL-continue-v2 curriculum
attempt on the small BIRD pool, configs/bird_adapt_rl_v2_phase*.yaml): flat
mode gives every wrong-but-executing completion in a group the identical
0.1, so when true exact-match success is rare (as it was on this pool --
health_log.jsonl showed train_reward_mean stuck at 0.05-0.20 the entire
run), most groups still end up with near-zero variance even with
partial_credit on, since "executes vs errors" alone doesn't distinguish a
near-miss from a wildly wrong query. Graduated mode instead scales the
wrong-but-executing bonus by row-overlap with gold (multiset intersection
over gold's row count, same order-insensitive comparison rows_match already
uses), between graduated_floor and graduated_max -- still capped well below
1.0, so a correct completion always outscores every incorrect one
regardless of overlap, same invariant as flat mode. This is a deliberately
narrow, hard-to-game signal: it rewards literally returning more of the
right rows, not any proxy that a model could satisfy without getting closer
to correct.

Both modes default to values that reproduce the original strict binary
reward (partial_credit=0.0, mode="flat") if not explicitly configured, so
existing configs are unaffected.
"""

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
