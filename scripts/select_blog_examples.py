"""select_blog_examples.py

Turns error_taxonomy.py's output into blog-ready material.

For a given arm's *_taxonomy.json, this pulls the real stored example(s)
per failure category (error_taxonomy.py already keeps up to 5 real
gold_sql/pred_sql pairs per category), looks up each example's
natural-language question from the matching *_raw.jsonl (taxonomy.json
only carries index/db_id/gold_sql/pred_sql -- generate_sql.py's raw.jsonl
has the question, in the same row order eval_sql.py assigned as `index`),
re-executes both gold_sql and pred_sql against the real per-example SQLite
database (same db_dir convention eval_sql.py uses:
<db_dir>/<db_id>/<db_id>.sqlite), and writes one markdown file with a
"gold vs predicted" section per category: question, both queries, and
both result tables (or the raw SQLite error string when a query fails to
execute).

Everything here is a real dev-set example with error_taxonomy.py's own
category assignment -- nothing is hand-written or simulated.

Usage:
  python scripts/select_blog_examples.py \
      --taxonomy runs/sft_qwen2.5coder3b_eval/spider_dev_taxonomy.json \
      --raw runs/sft_qwen2.5coder3b_eval/spider_dev_raw.jsonl \
      --db-dir data/spider_data/database \
      --label "SFT arm, Spider-dev" \
      --out runs/blog_artifacts/sft_spider_dev_gallery.md
"""

import argparse
import json
import os
import sqlite3

from error_taxonomy import CATEGORY_PRIORITY

CATEGORY_LABELS = {
    "syntax_error": "Syntax error",
    "schema_table_error": "Schema reference error (hallucinated table)",
    "schema_column_error": "Schema reference error (hallucinated column)",
    "timeout": "Timeout",
    "other_execution_error": "Other execution error",
    "table_reference_mismatch": "Table reference mismatch (join/schema linking)",
    "join_structure_mismatch": "Join error",
    "subquery_structure_mismatch": "Nested query error",
    "aggregation_mismatch": "Aggregation error",
    "orderby_limit_mismatch": "Order/limit mismatch",
    "other_wrong_result": "Other wrong result (e.g. value grounding)",
}


def load_raw_questions(raw_path):
    """index -> question, in the same row order eval_sql.py used for `index`."""
    questions = []
    with open(raw_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            questions.append(json.loads(line)["question"])
    return questions


def run_sql(db_dir, db_id, sql, timeout_sec=10):
    db_path = os.path.join(db_dir, db_id, f"{db_id}.sqlite")
    uri = f"file:{db_path}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
        conn.execute(f"PRAGMA busy_timeout = {timeout_sec * 1000}")
        cur = conn.cursor()
        cur.execute(sql)
        columns = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchall()
        conn.close()
        return {"ok": True, "columns": columns, "rows": rows}
    except sqlite3.Error as e:
        return {"ok": False, "error": str(e)}


def render_table_md(result):
    if not result["ok"]:
        return f"SQL error: `{result['error']}`"
    if not result["rows"]:
        return "_(0 rows)_"
    header = "| " + " | ".join(result["columns"]) + " |"
    sep = "|" + "|".join("---" for _ in result["columns"]) + "|"
    body = "\n".join(
        "| " + " | ".join(str(v) for v in row) + " |" for row in result["rows"][:10]
    )
    truncated = "\n\n_(truncated to first 10 of {} rows)_".format(len(result["rows"])) if len(result["rows"]) > 10 else ""
    return f"{header}\n{sep}\n{body}{truncated}"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--taxonomy", required=True, help="*_taxonomy.json from error_taxonomy.py")
    parser.add_argument("--raw", required=True, help="matching *_raw.jsonl from generate_sql.py")
    parser.add_argument("--db-dir", required=True, help="directory of per-db_id sqlite files")
    parser.add_argument("--label", default=None, help="heading label, defaults to --taxonomy path")
    parser.add_argument("--out", required=True)
    parser.add_argument("--per-category", type=int, default=1, help="examples to render per category")
    args = parser.parse_args()

    with open(args.taxonomy) as f:
        taxonomy = json.load(f)
    questions = load_raw_questions(args.raw)

    label = args.label or args.taxonomy
    total = taxonomy["total_examples"]
    exec_acc = taxonomy["n_correct"] / total if total else 0.0

    lines = [f"# Error gallery: {label}", ""]
    lines.append(
        f"{taxonomy['n_incorrect']} incorrect / {total} total "
        f"(execution accuracy {exec_acc:.4f}). Real dev-set examples, categorized by "
        "scripts/error_taxonomy.py -- nothing below is hand-written or simulated."
    )
    lines.append("")

    any_category = False
    for cat in CATEGORY_PRIORITY:
        examples = taxonomy["examples"].get(cat, [])
        if not examples:
            continue
        any_category = True
        m = taxonomy["multi_label"][cat]
        lines.append(f"## {CATEGORY_LABELS.get(cat, cat)}")
        lines.append(f"_{m['count']} of {taxonomy['n_incorrect']} incorrect predictions ({m['rate_of_incorrect']:.1%})_")
        lines.append("")
        for ex in examples[: args.per_category]:
            question = questions[ex["index"]] if ex["index"] < len(questions) else "(question unavailable)"
            lines.append(f"**Question** (`{ex['db_id']}`): {question}")
            lines.append("")
            lines.append("Gold SQL:")
            lines.append(f"```sql\n{ex['gold_sql']}\n```")
            lines.append(render_table_md(run_sql(args.db_dir, ex["db_id"], ex["gold_sql"])))
            lines.append("")
            lines.append("Predicted SQL:")
            lines.append(f"```sql\n{ex['pred_sql']}\n```")
            lines.append(render_table_md(run_sql(args.db_dir, ex["db_id"], ex["pred_sql"])))
            lines.append("")
        lines.append("---")
        lines.append("")

    if not any_category:
        lines.append("_No incorrect predictions with stored examples -- nothing to show._")

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w") as f:
        f.write("\n".join(lines))
    print(f"Wrote {args.out}")


if __name__ == "__main__":
    main()
