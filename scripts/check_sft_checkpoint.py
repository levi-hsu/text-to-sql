"""Pre-RL sanity check on an SFT checkpoint (see plan.md, RL arm).

RL is initialized from the SFT checkpoint and can only refine behavior the
checkpoint already exhibits -- it will amplify, not fix, a checkpoint that is
already degenerate (collapsed to a handful of templates, hallucinating schema
elements, or padding/truncating outputs). This script is meant to run once,
after train_sft.py + generate_sql.py + eval_sql.py have produced results for
the SFT arm on Spider-dev, and before scripts/train_rl.py starts. It checks
five things beyond plain execution accuracy, which eval_sql.py already
reports but does not diagnose:

  1. execution accuracy / predicted-query execution-error rate (pulled
     straight from eval_sql.py's summary, repeated here for one-report
     convenience)
  2. degenerate-output rate: predictions that are empty, a placeholder
     ("SELECT 1", "SELECT 1=0" -- see generate_sql.py's extract_sql fallback),
     or otherwise trivially short
  3. output length distribution (char and whitespace-token counts), to catch
     truncation (hitting max_new_tokens) or runaway verbosity
  4. template diversity: predictions are normalized (literals and whitespace
     stripped) and grouped; low distinct-template ratio or low entropy means
     the checkpoint has collapsed onto a few canned query shapes rather than
     actually conditioning on the question
  5. schema-hallucination rate: quoted identifiers in the predicted SQL that
     do not exist in that example's db_id schema (tables.json), which a
     naive execution-accuracy number can hide when the hallucinated query
     happens not to error out. Table aliases (e.g. "students" AS "s") are
     resolved first, so "s"."name" is checked against students's own
     columns rather than either flagging "s" itself or checking "name"
     against the whole database's pooled column set -- see the NOTE in
     check_schema_hallucination() for what this still cannot catch, and
     scripts/calibrate_hallucination_floor.py for measuring this check's
     own residual false-positive rate against gold SQL

This is a heuristic, regex-based check (matching the identifier-quoting
convention schema_utils.py and prompt_template.py already use), not a full
SQL parser -- see NOTE in check_schema_hallucination() for what it misses.

--max-hallucination-rate's default (0.03) is not arbitrary: it was set by
running scripts/calibrate_hallucination_floor.py against Spider-dev's own
gold SQL, which measures this check's own false-positive rate on queries
that are correct by construction. That floor is currently 0.0000 (0/1034)
after fixing an earlier version of this check that misread Spider gold's
double-quoted string literals, e.g. WHERE "Airline" = "JetBlue Airways", as
identifiers -- SQLite accepts double-quoted string literals as a fallback
when a token doesn't match a real identifier, and Spider's gold SQL relies
on that. The zero-shot baseline arm was checked too and is uninformative as
a calibration point: 0/1034 of its predictions contain any double-quoted
token at all, so its 0.0000 rate reflects the check being blind to that
arm's output, not the baseline model actually grounding its column
references. 0.03 leaves a small margin above the measured 0.0000 floor for
the check's documented residual gaps (see check_schema_hallucination's NOTE)
on real model output, which is shaped differently from Spider's gold SQL and
may hit those gaps more often than gold does -- it is not intended to
tolerate real hallucination from the model being evaluated. Re-run the
calibration script after the SFT arm's predictions exist, since a
model-specific floor (e.g. checking the SFT model's *correct* predictions,
where available) would be a tighter and more direct calibration than the
gold-SQL floor alone.

Usage:
  python scripts/check_sft_checkpoint.py \
      --raw runs/sft_qwen2.5coder3b_eval/spider_dev_raw.jsonl \
      --results runs/sft_qwen2.5coder3b_eval/spider_dev_results.json \
      --tables data/spider_data/tables.json \
      --output runs/sft_qwen2.5coder3b_eval/sft_sanity_report.json

Exits 0 if all checks pass their threshold, 1 otherwise, so it can gate a
run_rl.sh script (`python scripts/check_sft_checkpoint.py ... || exit 1`).
"""

import argparse
import json
import math
import re
from collections import Counter
from typing import Dict, List, Set

from schema_utils import load_tables

DEGENERATE_PATTERNS = {"select 1", "select 1=0", "select 1 = 0", ""}
QUOTED_IDENT_RE = re.compile(r'"([^"]+)"')
STRING_LITERAL_RE = re.compile(r"'(?:[^']|'')*'")  # '' inside a literal is SQL's escaped single quote
NUMBER_LITERAL_RE = re.compile(r"(?<![A-Za-z0-9_])\d+(\.\d+)?(?![A-Za-z0-9_])")

# "real_table" AS "alias" -- only counted as a table-alias binding if the
# left-hand side is an actual table name for that db_id (checked at call
# site), so this does not also swallow ordinary column aliases like
# SELECT "name" AS "n".
TABLE_ALIAS_RE = re.compile(r'"([^"]+)"\s+AS\s+"([^"]+)"', re.IGNORECASE)
# "left"."right" -- the qualified-reference pattern this whole check exists
# to resolve, e.g. "s"."name" or "students"."name".
DOTTED_REF_RE = re.compile(r'"([^"]+)"\s*\.\s*"([^"]+)"')

# Operators/keywords after which a double-quoted token is a value, not an
# identifier -- SQLite accepts double-quoted string literals as a fallback
# when the token doesn't resolve to a real identifier (its own documented
# quirk), and Spider's gold SQL relies on this for filter values, e.g.
# WHERE "Airline" = "JetBlue Airways". A literal is never written with a
# dot, so this only applies to standalone (non-dotted) tokens.
_COMPARISON_OPERATORS = ("<>", "!=", "<=", ">=", "=", "<", ">")
_VALUE_KEYWORDS = {"like", "is"}

# IN ("a", "b", "c") -- each item is a value, but only the first is directly
# preceded by an operator/keyword; the rest are preceded by a comma, which
# is indistinguishable on its own from a comma in a GROUP BY/ORDER BY column
# list. Matching the whole (non-nested) parenthesized span after IN and
# treating every quoted token inside it as a value avoids that ambiguity.
IN_LIST_RE = re.compile(r"\bIN\s*\(([^()]*)\)", re.IGNORECASE)

# AS "alias" -- declares a new name (table alias or output column alias); it
# is never itself a reference to an existing schema identifier, so it should
# never be checked against the schema regardless of what precedes the AS.
# Deliberately broader than TABLE_ALIAS_RE/build_alias_map, which only
# resolve bindings whose left-hand side is an actual table name -- this also
# catches subquery aliases like (SELECT ...) AS "x", where "x" cannot be
# resolved to a real table but is still a legitimate declared name.
DECLARED_ALIAS_RE = re.compile(r'\bAS\s+"([^"]+)"', re.IGNORECASE)


def _preceded_by_value_context(text: str, match_start: int) -> bool:
    prefix = text[:match_start].rstrip()
    if not prefix:
        return False
    if prefix.endswith(_COMPARISON_OPERATORS):
        return True
    last_word = prefix.split()[-1].strip("(),").lower() if prefix.split() else ""
    return last_word in _VALUE_KEYWORDS


def _in_list_spans(text: str) -> List[tuple]:
    return [m.span(1) for m in IN_LIST_RE.finditer(text)]


def load_raw(raw_path: str) -> List[dict]:
    records = []
    with open(raw_path, "r") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def is_degenerate(pred_sql: str) -> bool:
    normalized = pred_sql.strip().rstrip(";").strip().lower()
    return normalized in DEGENERATE_PATTERNS or len(normalized) < 8


def length_stats(records: List[dict]) -> dict:
    char_lens = sorted(len(r["pred_sql"]) for r in records)
    tok_lens = sorted(len(r["pred_sql"].split()) for r in records)

    def pctile(sorted_vals: List[int], p: float) -> float:
        if not sorted_vals:
            return 0.0
        idx = min(len(sorted_vals) - 1, int(round(p * (len(sorted_vals) - 1))))
        return sorted_vals[idx]

    return {
        "char_len": {
            "mean": sum(char_lens) / len(char_lens) if char_lens else 0.0,
            "p5": pctile(char_lens, 0.05),
            "median": pctile(char_lens, 0.5),
            "p95": pctile(char_lens, 0.95),
        },
        "token_len": {
            "mean": sum(tok_lens) / len(tok_lens) if tok_lens else 0.0,
            "p5": pctile(tok_lens, 0.05),
            "median": pctile(tok_lens, 0.5),
            "p95": pctile(tok_lens, 0.95),
        },
    }


def normalize_template(pred_sql: str) -> str:
    """Strip literals so structurally-identical queries collapse together.

    'SELECT name FROM t WHERE id = 3' and 'SELECT name FROM t WHERE id = 91'
    normalize to the same template; two queries with different table/column
    references do not. This is what template diversity is measured over.
    """
    t = STRING_LITERAL_RE.sub("<STR>", pred_sql)
    t = NUMBER_LITERAL_RE.sub("<NUM>", t)
    t = " ".join(t.lower().split())
    return t


def template_diversity(records: List[dict]) -> dict:
    templates = [normalize_template(r["pred_sql"]) for r in records]
    counts = Counter(templates)
    n = len(templates)
    distinct_ratio = len(counts) / n if n else 0.0

    entropy = 0.0
    for c in counts.values():
        p = c / n
        entropy -= p * math.log2(p)
    max_entropy = math.log2(len(counts)) if len(counts) > 1 else 1.0
    normalized_entropy = entropy / max_entropy if max_entropy else 0.0

    top5 = counts.most_common(5)
    return {
        "distinct_templates": len(counts),
        "distinct_ratio": distinct_ratio,
        "entropy_bits": entropy,
        "normalized_entropy": normalized_entropy,
        "top_templates": [{"template": t, "count": c} for t, c in top5],
    }


def schema_identifiers(table_entry: dict) -> Set[str]:
    """All table and column names (original casing) valid for one db_id, pooled
    across every table. Used as the fallback check for references this
    function cannot resolve to a specific table (see check_schema_hallucination).
    """
    idents = set(table_entry["table_names_original"])
    for _table_idx, col_name in table_entry["column_names_original"]:
        if col_name != "*":
            idents.add(col_name)
    return idents


def table_columns(table_entry: dict, table_name: str) -> Set[str]:
    """Column names (original casing) belonging to one specific table."""
    table_names = table_entry["table_names_original"]
    if table_name not in table_names:
        return set()
    table_idx = table_names.index(table_name)
    return {
        col_name
        for t_idx, col_name in table_entry["column_names_original"]
        if t_idx == table_idx and col_name != "*"
    }


def build_alias_map(pred_sql: str, table_names: Set[str]) -> Dict[str, str]:
    """Resolve 'AS' aliases to the real table they name, e.g. {"s": "students"}.

    Only accepts a binding "X" AS "Y" as a table alias if X is an actual
    table name for this db_id -- this is what keeps it from misreading an
    ordinary column alias like SELECT "name" AS "n" as a table binding,
    without needing to locate the FROM/JOIN clause.
    """
    return {alias: real for real, alias in TABLE_ALIAS_RE.findall(pred_sql) if real in table_names}


def check_schema_hallucination(records: List[dict], tables_by_db: Dict[str, dict]) -> dict:
    """Option B: resolve "alias"."col" references to the specific table the
    alias is bound to (or the table itself, if unaliased), and check "col"
    against that table's own columns rather than the whole database's pooled
    column set. This both removes the Option-A false positive on the alias
    token itself (e.g. "s" in "s"."name" is no longer checked against the
    schema as if it were a column) and catches a class of error Option A
    cannot: a real column name attached to the wrong table, e.g. "s"."dept_id"
    where dept_id exists in the schema but not on the table "s" is aliased to.

    NOTE: still heuristic, not a full parser. Three known gaps: (1) only
    double-quoted identifiers are seen at all, so unquoted/backtick/bracket
    references are invisible to this check (undercounts); (2) when the alias
    cannot be resolved -- CTEs, subquery aliases, or any "AS" binding whose
    left-hand side is not a base-table name -- this falls back to checking
    the column half against the whole-schema pool, same as before, rather
    than flagging the unresolved alias itself (residual overcount risk on
    that fallback path only); (3) standalone quoted tokens immediately
    preceded by a comparison operator or LIKE/IN/IS are treated as string
    literals, not identifiers, and skipped entirely (calibration against
    gold SQL showed this is the dominant source of false positives, e.g.
    WHERE "Airline" = "JetBlue Airways" -- see
    scripts/calibrate_hallucination_floor.py), but this is a positional
    heuristic, not a parse, so an unusual literal position could still slip
    through unflagged as a value when it was in fact meant as an identifier.
    Run scripts/calibrate_hallucination_floor.py against gold SQL to measure
    this check's own residual false-positive rate empirically instead of
    assuming it is zero.
    """
    n_with_hallucination = 0
    examples = []
    for i, r in enumerate(records):
        db_id = r["db_id"]
        table_entry = tables_by_db.get(db_id)
        if table_entry is None:
            continue  # db_id not in tables.json; skip rather than false-flag
        pred_sql = r["pred_sql"]
        table_names = set(table_entry["table_names_original"])
        valid = schema_identifiers(table_entry)
        alias_map = build_alias_map(pred_sql, table_names)

        bogus = set()
        dotted_pairs = DOTTED_REF_RE.findall(pred_sql)
        dotted_tokens = set()
        for left, right in dotted_pairs:
            dotted_tokens.add(left)
            dotted_tokens.add(right)
            resolved_table = alias_map.get(left, left if left in table_names else None)
            if resolved_table is not None:
                if right not in table_columns(table_entry, resolved_table):
                    bogus.add(f'"{left}"."{right}"')
            else:
                # Unresolved alias (CTE, subquery, etc.): can't pin down which
                # table, so fall back to the lenient whole-schema check on the
                # column half only, and don't flag the alias token itself.
                if right not in valid:
                    bogus.add(f'"{left}"."{right}" (unresolved alias)')

        # Standalone quoted tokens not part of a dotted pair and not an
        # alias name: check against the whole-schema pool, unless the token
        # sits in a value position (e.g. right after "=" or "LIKE", or
        # inside an IN (...) list), in which case it's a string literal, not
        # an identifier, and skipped.
        dotted_spans = [m.span() for m in DOTTED_REF_RE.finditer(pred_sql)]
        in_list_spans = _in_list_spans(pred_sql)
        declared_aliases = set(DECLARED_ALIAS_RE.findall(pred_sql))
        for m in QUOTED_IDENT_RE.finditer(pred_sql):
            tok = m.group(1)
            if any(start <= m.start() < end for start, end in dotted_spans):
                continue  # part of a dotted "a"."b" reference, handled above
            if tok in declared_aliases:
                continue  # this is a declared name (table or column alias)
            if any(start <= m.start() < end for start, end in in_list_spans):
                continue  # inside IN (...), e.g. WHERE "x" IN ("Alice", "Bob")
            if _preceded_by_value_context(pred_sql, m.start()):
                continue  # e.g. WHERE "Airline" = "JetBlue Airways" -- a literal
            if tok not in valid:
                bogus.add(f'"{tok}"')

        if bogus:
            n_with_hallucination += 1
            if len(examples) < 10:
                examples.append({"index": i, "db_id": db_id, "bogus_identifiers": sorted(bogus)})
    total = len(records)
    return {
        "rate": n_with_hallucination / total if total else 0.0,
        "count": n_with_hallucination,
        "total": total,
        "examples": examples,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw", required=True, help="*_raw.jsonl from generate_sql.py")
    parser.add_argument("--results", required=True, help="*_results.json from eval_sql.py")
    parser.add_argument("--tables", required=True, help="tables.json for the same dataset")
    parser.add_argument("--output", default=None, help="Where to write the JSON report")
    parser.add_argument("--min-ex-accuracy", type=float, default=0.0,
                         help="Fail if execution_accuracy is below this (0 disables the gate)")
    parser.add_argument("--max-degenerate-rate", type=float, default=0.10)
    parser.add_argument("--min-distinct-ratio", type=float, default=0.30)
    parser.add_argument(
        "--max-hallucination-rate", type=float, default=0.03,
        help="Calibrated against gold SQL's measured floor (0.0000); see module docstring.",
    )
    args = parser.parse_args()

    raw_records = load_raw(args.raw)
    with open(args.results, "r") as f:
        eval_output = json.load(f)
    summary = eval_output["summary"]
    tables_by_db = load_tables(args.tables)

    if len(raw_records) != summary["total"]:
        raise ValueError(
            f"raw.jsonl has {len(raw_records)} records but results.json summary reports "
            f"total={summary['total']}; these must come from the same generate_sql.py / "
            "eval_sql.py run."
        )

    degenerate = [r for r in raw_records if is_degenerate(r["pred_sql"])]
    degenerate_rate = len(degenerate) / len(raw_records) if raw_records else 0.0

    report = {
        "execution": {
            "execution_accuracy": summary["execution_accuracy"],
            "pred_execution_error_rate": (
                summary["pred_execution_errors"] / summary["total"] if summary["total"] else 0.0
            ),
            "gold_execution_errors": summary["gold_execution_errors"],
        },
        "degenerate_output_rate": degenerate_rate,
        "length": length_stats(raw_records),
        "diversity": template_diversity(raw_records),
        "schema_hallucination": check_schema_hallucination(raw_records, tables_by_db),
    }

    checks = [
        ("execution_accuracy", report["execution"]["execution_accuracy"],
         ">=", args.min_ex_accuracy),
        ("degenerate_output_rate", report["degenerate_output_rate"],
         "<=", args.max_degenerate_rate),
        ("diversity.distinct_ratio", report["diversity"]["distinct_ratio"],
         ">=", args.min_distinct_ratio),
        ("schema_hallucination.rate", report["schema_hallucination"]["rate"],
         "<=", args.max_hallucination_rate),
    ]

    passed = True
    print("SFT checkpoint sanity check")
    print("=" * 60)
    for name, value, op, threshold in checks:
        ok = (value >= threshold) if op == ">=" else (value <= threshold)
        if op == ">=" and threshold == 0.0:
            ok = True  # gate disabled
        status = "OK" if ok else "FAIL"
        print(f"[{status}] {name} = {value:.4f} (require {op} {threshold})")
        passed = passed and ok

    print("-" * 60)
    print(f"pred_execution_error_rate = {report['execution']['pred_execution_error_rate']:.4f}")
    print(f"length (tokens) median/p95 = "
          f"{report['length']['token_len']['median']:.0f} / {report['length']['token_len']['p95']:.0f}")
    print(f"diversity normalized_entropy = {report['diversity']['normalized_entropy']:.4f}")
    if report["schema_hallucination"]["examples"]:
        print("Sample hallucinated-identifier cases (see report for full list):")
        for ex in report["schema_hallucination"]["examples"][:3]:
            print(f"  index={ex['index']} db_id={ex['db_id']} bogus={ex['bogus_identifiers']}")

    if args.output:
        with open(args.output, "w") as f:
            json.dump(report, f, indent=2)
        print(f"\nWrote full report to {args.output}")

    print("\nRESULT:", "PASS -- checkpoint looks sane enough to start RL from."
          if passed else "FAIL -- inspect the report before starting RL.")
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
