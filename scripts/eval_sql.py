"""
Execution accuracy (EX) verifier: run a predicted SQL query and a gold SQL
query against the same SQLite database, and check whether they return the
same result.

Known simplification, stated up front: this compares result rows as a
row-order-insensitive multiset of tuples (so ORDER BY differences don't
matter), but it is column-order-sensitive (SELECT b, a is NOT treated as
equal to SELECT a, b even though they contain the same information). Spider's
own official evaluator does a more elaborate SQL-structure-aware comparison
to handle that case and a few others (e.g. semantically-equivalent but
differently-shaped queries). This is a deliberate simplification to keep the
verifier small, fast, and dependency-free, appropriate for reward computation
at RL training scale; if a precise apples-to-apples comparison against
published Spider EX numbers is needed later, swap in the official Spider
evaluation script (github.com/taoyds/spider, evaluation.py) instead.

Also used as the RL reward function later (PLAN.md Stage 2): a binary EX
score from execution_match() is exactly the "binary execution accuracy, no
reward shaping" reward described there.

Cross-platform note: the query timeout used to be implemented with
signal.SIGALRM, which is POSIX-only and does not exist on Windows (this was
a real bug when the project was Mac-only; fixed here using a
threading.Timer that calls sqlite3.Connection.interrupt() instead, which
works identically on macOS, Linux, and Windows). The database file URI is
also now built with Path.as_uri() rather than manual string formatting,
since a naive f"file:{path}" breaks on Windows paths (backslashes, drive
letters) but as_uri() handles both platforms correctly.

Usage as a library:
    from eval_sql import execution_match
    ok, detail = execution_match(db_path, gold_sql, pred_sql)

Usage as a CLI, scoring a predictions file against an eval JSONL produced by
prepare_spider_data.py:
    python3 scripts/eval_sql.py \
        --eval-file data/eval/dev.jsonl \
        --pred-file predictions.jsonl \
        --spider-dir spider_data
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import threading
from collections import Counter
from pathlib import Path


class QueryTimeout(Exception):
    pass


def _run_query(db_path: Path, sql: str, timeout_s: float) -> list[tuple]:
    """Execute sql against db_path, return all result rows as a list of
    tuples. Raises on SQL error or timeout; caller is responsible for
    catching. Cross-platform (no signal.SIGALRM, which doesn't exist on
    Windows): a background timer calls conn.interrupt() if the query is
    still running after timeout_s seconds, which sqlite3 turns into an
    OperationalError inside the running query."""
    # Open read-only so a buggy/malicious generated query can't mutate the
    # shared database file. as_uri() produces a correct file:// URI on both
    # POSIX and Windows (drive letters, backslashes handled correctly),
    # unlike a manual f"file:{path}" string.
    uri = db_path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=timeout_s)
    timed_out = threading.Event()

    def _watchdog():
        timed_out.set()
        conn.interrupt()

    timer = threading.Timer(timeout_s, _watchdog)
    timer.start()
    try:
        cur = conn.cursor()
        cur.execute(sql)
        rows = cur.fetchall()
        return [tuple(r) for r in rows]
    except sqlite3.OperationalError:
        if timed_out.is_set():
            raise QueryTimeout()
        raise
    finally:
        timer.cancel()
        conn.close()


def execution_match(db_path: Path, gold_sql: str, pred_sql: str, timeout_s: float = 5.0) -> tuple[bool, str]:
    """Returns (is_match, detail). is_match is False (not an exception) for
    any failure mode: predicted SQL doesn't parse/execute, times out, or
    executes but returns a different result set than gold."""
    try:
        gold_rows = _run_query(db_path, gold_sql, timeout_s)
    except Exception as e:
        # Gold query itself failing means a data/script bug, not a model
        # failure -- surface it loudly rather than silently scoring 0.
        return False, f"GOLD_QUERY_ERROR: {e!r}"

    try:
        pred_rows = _run_query(db_path, pred_sql, timeout_s)
    except QueryTimeout:
        return False, "PRED_TIMEOUT"
    except Exception as e:
        return False, f"PRED_ERROR: {e!r}"

    if Counter(gold_rows) == Counter(pred_rows):
        return True, "MATCH"
    return False, f"MISMATCH: gold_rows={len(gold_rows)} pred_rows={len(pred_rows)}"


def extract_sql(model_output: str) -> str:
    """Best-effort cleanup of raw model output into a single SQL statement:
    strip markdown code fences if the model added them anyway, take text up
    to (and including) the first semicolon if present."""
    text = model_output.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("sql"):
            text = text[3:]
    text = text.strip()
    if ";" in text:
        text = text[: text.index(";") + 1]
    return text.strip()


def score_file(eval_file: Path, pred_file: Path, spider_dir: Path) -> dict:
    eval_records = [json.loads(l) for l in open(eval_file, encoding="utf-8")]
    pred_records = [json.loads(l) for l in open(pred_file, encoding="utf-8")]
    if len(eval_records) != len(pred_records):
        raise SystemExit(
            f"eval file has {len(eval_records)} records, pred file has {len(pred_records)} -- "
            "must be line-aligned, one prediction per eval example, same order."
        )

    n_correct = 0
    per_db = Counter()
    per_db_correct = Counter()
    details = []
    for eval_r, pred_r in zip(eval_records, pred_records):
        db_path = spider_dir / eval_r["db_path"]
        pred_sql = extract_sql(pred_r["prediction"])
        ok, detail = execution_match(db_path, eval_r["gold_sql"], pred_sql)
        per_db[eval_r["db_id"]] += 1
        if ok:
            n_correct += 1
            per_db_correct[eval_r["db_id"]] += 1
        details.append({"db_id": eval_r["db_id"], "question": eval_r["question"], "match": ok, "detail": detail})

    ex = n_correct / len(eval_records) if eval_records else 0.0
    return {
        "n": len(eval_records),
        "n_correct": n_correct,
        "execution_accuracy": ex,
        "per_db_accuracy": {db: per_db_correct[db] / per_db[db] for db in per_db},
        "details": details,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--eval-file", required=True, help="JSONL from prepare_spider_data.py, e.g. data/eval/dev.jsonl")
    ap.add_argument("--pred-file", required=True, help="JSONL, one line per eval example, each with a 'prediction' field")
    ap.add_argument("--spider-dir", default="spider_data", help="Root of the raw Spider download (contains database/)")
    ap.add_argument("--out", default=None, help="Optional path to write full per-example results as JSON")
    args = ap.parse_args()

    result = score_file(Path(args.eval_file), Path(args.pred_file), Path(args.spider_dir))
    print(f"n={result['n']}  n_correct={result['n_correct']}  execution_accuracy={result['execution_accuracy']:.4f}")
    if args.out:
        Path(args.out).write_text(json.dumps(result, indent=2))
        print(f"Full results written to {args.out}")


if __name__ == "__main__":
    main()
