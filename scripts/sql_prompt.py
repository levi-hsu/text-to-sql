"""
Shared schema-linearization and prompt-building code for the text-to-SQL
project. Used by both prepare_spider_data.py (building SFT/eval JSONL files)
and run_baseline_eval.py (building prompts at inference time), so the exact
same prompt format is guaranteed at training and evaluation time.

No third-party dependencies. Pure stdlib, so this file can be imported and
unit-tested anywhere, including outside the MLX/Apple Silicon environment.
"""

from __future__ import annotations

from typing import Any

SYSTEM_PROMPT = (
    "You are a text-to-SQL assistant. Given a SQLite database schema and a "
    "question, output a single valid SQLite SQL query that answers the "
    "question, using only the tables and columns given in the schema. "
    "Output only the SQL query, terminated with a semicolon, and nothing "
    "else -- no explanation, no markdown code fences."
)

_TYPE_MAP = {
    "text": "TEXT",
    "number": "INTEGER",
    "time": "DATE",
    "boolean": "BOOLEAN",
    "others": "TEXT",
}


def _sql_type(spider_type: str) -> str:
    return _TYPE_MAP.get(spider_type, "TEXT")


def build_schema_ddl(db_entry: dict[str, Any]) -> str:
    """Turn one entry of Spider's tables.json into a linearized CREATE TABLE
    DDL block: one CREATE TABLE statement per table, primary keys marked
    inline, foreign keys as FOREIGN KEY ... REFERENCES ... lines.

    db_entry is one element of the list loaded from tables.json.
    """
    table_names = db_entry["table_names_original"]
    column_names = db_entry["column_names_original"]  # [[table_idx, col_name], ...], index 0 is [-1, "*"]
    column_types = db_entry["column_types"]
    primary_keys = set(db_entry.get("primary_keys", []))
    foreign_keys = db_entry.get("foreign_keys", [])

    # global column index -> (table_idx, col_name)
    # global column index -> ref (table_idx, col_name) if this column is the "from" side of an FK
    fk_by_col: dict[int, tuple[int, str]] = {}
    for col_idx, ref_idx in foreign_keys:
        ref_table_idx, ref_col_name = column_names[ref_idx]
        fk_by_col[col_idx] = (ref_table_idx, ref_col_name)

    statements = []
    for table_idx, table_name in enumerate(table_names):
        col_lines = []
        fk_lines = []
        for global_idx, (t_idx, col_name) in enumerate(column_names):
            if t_idx != table_idx:
                continue
            col_type = _sql_type(column_types[global_idx])
            line = f"  {col_name} {col_type}"
            if global_idx in primary_keys:
                line += " PRIMARY KEY"
            col_lines.append(line)
            if global_idx in fk_by_col:
                ref_table_idx, ref_col_name = fk_by_col[global_idx]
                ref_table_name = table_names[ref_table_idx]
                fk_lines.append(
                    f"  FOREIGN KEY ({col_name}) REFERENCES {ref_table_name}({ref_col_name})"
                )
        body = ",\n".join(col_lines + fk_lines)
        statements.append(f"CREATE TABLE {table_name} (\n{body}\n);")

    return "\n\n".join(statements)


def build_user_message(schema_ddl: str, question: str) -> str:
    return (
        f"### Database schema:\n{schema_ddl}\n\n"
        f"### Question:\n{question}"
    )


def build_chat_messages(schema_ddl: str, question: str, gold_sql: str | None = None) -> list[dict[str, str]]:
    """Build the full chat-format message list. If gold_sql is given, an
    assistant turn is appended (for SFT training data); otherwise the list
    ends after the user turn (for inference)."""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_user_message(schema_ddl, question)},
    ]
    if gold_sql is not None:
        sql = gold_sql.strip()
        if not sql.endswith(";"):
            sql += ";"
        messages.append({"role": "assistant", "content": sql})
    return messages
