import argparse
import json
import os
import signal
import sqlite3
from collections import Counter
from typing import List, Optional, Tuple

from data_utils import load_gold_sql


class QueryTimeout(Exception):
    pass


def _timeout_handler(signum, frame):
    raise QueryTimeout()


def get_connection(db_dir: str, db_id: str, cache: dict) -> sqlite3.Connection:
    if db_id not in cache:
        db_path = os.path.join(db_dir, db_id, f"{db_id}.sqlite")
        if not os.path.exists(db_path):
            raise FileNotFoundError(f"No sqlite file for db_id '{db_id}' at {db_path}")
        uri = f"file:{db_path}?mode=ro"
        cache[db_id] = sqlite3.connect(uri, uri=True, check_same_thread=False)
    return cache[db_id]


def execute_query(
    conn: sqlite3.Connection, sql: str, timeout_sec: int
) -> Tuple[Optional[List[tuple]], Optional[str]]:
    """Run sql, returning (rows, None) on success or (None, error_str) on failure."""
    old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
    signal.alarm(timeout_sec)
    try:
        cur = conn.cursor()
        cur.execute(sql)
        rows = cur.fetchall()
        return rows, None
    except QueryTimeout:
        return None, "timeout"
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)


def rows_match(gold_rows: List[tuple], pred_rows: List[tuple], order_matters: bool) -> bool:
    if order_matters:
        return gold_rows == pred_rows
    try:
        return sorted(gold_rows) == sorted(pred_rows)
    except TypeError:
        # Mixed/unorderable types (e.g. None alongside str in the same column):
        # fall back to a multiset comparison over string reprs of each row.
        return Counter(map(str, gold_rows)) == Counter(map(str, pred_rows))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pred", required=True, help="Predictions file, one SQL query per line.")
    parser.add_argument("--gold", required=True, help="Gold file: 'SQL<TAB>db_id' per line.")
    parser.add_argument("--db-dir", required=True, help="Directory of per-db_id sqlite files.")
    parser.add_argument("--timeout", type=int, default=10, help="Per-query execution timeout, seconds.")
    parser.add_argument("--output", default=None, help="Where to write the JSON summary + per-example results.")
    args = parser.parse_args()

    gold_pairs = load_gold_sql(args.gold)
    with open(args.pred, "r") as f:
        pred_queries = [line.strip() for line in f if line.strip()]

    if len(pred_queries) != len(gold_pairs):
        raise ValueError(
            f"Line count mismatch: {len(pred_queries)} predictions vs {len(gold_pairs)} gold "
            "queries. generate_sql.py must produce exactly one line per dev.json example, in "
            "the same order as the *_gold.sql file."
        )

    conn_cache: dict = {}
    results = []
    n_correct = 0
    n_pred_exec_error = 0
    n_gold_exec_error = 0

    for i, ((gold_sql, db_id), pred_sql) in enumerate(zip(gold_pairs, pred_queries)):
        conn = get_connection(args.db_dir, db_id, conn_cache)
        order_matters = "order by" in gold_sql.lower()

        gold_rows, gold_err = execute_query(conn, gold_sql, args.timeout)
        pred_rows, pred_err = execute_query(conn, pred_sql, args.timeout)

        if gold_err is not None:
            n_gold_exec_error += 1
        if pred_err is not None:
            n_pred_exec_error += 1

        if gold_err is None and pred_err is None:
            match = rows_match(gold_rows, pred_rows, order_matters)
        else:
            match = False

        if match:
            n_correct += 1

        results.append(
            {
                "index": i,
                "db_id": db_id,
                "gold_sql": gold_sql,
                "pred_sql": pred_sql,
                "gold_error": gold_err,
                "pred_error": pred_err,
                "match": match,
            }
        )

    for conn in conn_cache.values():
        conn.close()

    total = len(results)
    ex_accuracy = n_correct / total if total else 0.0

    summary = {
        "total": total,
        "correct": n_correct,
        "execution_accuracy": ex_accuracy,
        "pred_execution_errors": n_pred_exec_error,
        "gold_execution_errors": n_gold_exec_error,
    }

    print(f"Execution accuracy: {n_correct}/{total} = {ex_accuracy:.4f}")
    print(f"Predictions that failed to execute: {n_pred_exec_error}/{total}")
    if n_gold_exec_error:
        print(
            f"WARNING: {n_gold_exec_error} gold queries failed to execute "
            "-- check for a data/DB mismatch, this is not a model error."
        )

    if args.output:
        os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
        with open(args.output, "w") as f:
            json.dump({"summary": summary, "results": results}, f, indent=2)
        print(f"Wrote detailed results to {args.output}")


if __name__ == "__main__":
    main()
