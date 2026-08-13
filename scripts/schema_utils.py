"""Build CREATE TABLE-style schema prompts from Spider/BIRD tables.json.

Spider and BIRD both ship a tables.json with the same structure: for each
db_id, a flat list of (table_idx, column_name) pairs in column_names /
column_names_original, a parallel column_types list, primary_keys (a flat
list of global column indices), and foreign_keys (a list of
[col_idx, ref_col_idx] pairs). This module turns that structure into
readable CREATE TABLE statements, which is what gets fed to the model as
schema context -- this is the same representation used in the text-to-SQL
literature (e.g. DIN-SQL, DAIL-SQL), not the raw schema.sql (which also
carries INSERT rows the model does not need and which would blow up the
prompt length for no benefit).
"""

import json
from typing import Dict, List


def load_tables(tables_path: str) -> Dict[str, dict]:
    """Load a tables.json file into a dict keyed by db_id."""
    with open(tables_path, "r") as f:
        tables = json.load(f)
    return {t["db_id"]: t for t in tables}


def build_schema_prompt(table_entry: dict) -> str:
    """Render one tables.json entry as CREATE TABLE statements.

    table_entry is a single dict from tables.json, e.g. the value for one
    db_id returned by load_tables().
    """
    table_names = table_entry["table_names_original"]
    column_names = table_entry["column_names_original"]
    column_types = table_entry["column_types"]
    # Spider's tables.json always lists primary_keys as flat column indices.
    # BIRD's does too, except for composite keys, which it represents as a
    # nested list of column indices (e.g. [1, 4, [19, 20]] means columns 19
    # and 20 together form one composite key). Flatten before building the
    # set so both formats land in the same flat set of column indices; the
    # per-column loop below already groups multiple flagged columns in the
    # same table into a single "PRIMARY KEY (a, b)" line, so no other change
    # is needed to render composite keys correctly.
    primary_keys = set()
    for pk in table_entry.get("primary_keys", []):
        if isinstance(pk, list):
            primary_keys.update(pk)
        else:
            primary_keys.add(pk)
    foreign_keys = table_entry.get("foreign_keys", [])

    # column_names[0] is always [-1, "*"] (the wildcard column) -- skip it.
    # Group the remaining columns by their owning table index.
    columns_by_table: Dict[int, List[int]] = {i: [] for i in range(len(table_names))}
    for col_idx, (table_idx, _col_name) in enumerate(column_names):
        if table_idx == -1:
            continue
        columns_by_table[table_idx].append(col_idx)

    # col_idx -> "table_name"."column_name", used to render REFERENCES.
    def col_ref(col_idx: int) -> str:
        t_idx, c_name = column_names[col_idx]
        return f'"{table_names[t_idx]}"("{c_name}")'

    statements = []
    for table_idx, table_name in enumerate(table_names):
        lines = []
        pk_cols = []
        fk_lines = []

        for col_idx in columns_by_table[table_idx]:
            _t_idx, col_name = column_names[col_idx]
            col_type = column_types[col_idx]
            lines.append(f'"{col_name}" {col_type}')
            if col_idx in primary_keys:
                pk_cols.append(col_name)

        for col_idx, ref_col_idx in foreign_keys:
            if col_idx in columns_by_table[table_idx]:
                _t_idx, col_name = column_names[col_idx]
                fk_lines.append(f'FOREIGN KEY ("{col_name}") REFERENCES {col_ref(ref_col_idx)}')

        if pk_cols:
            pk_str = ", ".join(f'"{c}"' for c in pk_cols)
            lines.append(f"PRIMARY KEY ({pk_str})")
        lines.extend(fk_lines)

        body = ",\n".join(lines)
        statements.append(f'CREATE TABLE "{table_name}" (\n{body}\n);')

    return "\n\n".join(statements)


def get_schema_str(db_id: str, tables_by_db: Dict[str, dict]) -> str:
    """Convenience wrapper: db_id -> rendered schema string."""
    if db_id not in tables_by_db:
        raise KeyError(f"db_id '{db_id}' not found in tables.json")
    return build_schema_prompt(tables_by_db[db_id])
