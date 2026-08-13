"""Pull a single-line SQL statement out of raw model output.

Split out of generate_sql.py so this pure regex logic can be reused by
rl_reward.py's execution-based reward without dragging torch/transformers
into the reward path's import graph -- the reward function runs once per
completion, many times per training step, and has no need for a GPU stack
of its own (the model that produced the completion is already loaded
elsewhere in the training process).
"""

import re

FENCE_RE = re.compile(r"```(?:sql)?\s*([\s\S]*?)```", re.IGNORECASE)
SQL_START_RE = re.compile(r"\b(SELECT|WITH)\b", re.IGNORECASE)


def extract_sql(text: str) -> str:
    """Handles the common failure modes of instruction-following models on
    this task: wrapping the query in a markdown code fence, prefacing the
    query with a preamble ("The answer is: SELECT ..."), or continuing to
    generate explanation/commentary after the query.
    """
    text = text.strip()
    m = FENCE_RE.search(text)
    if m:
        text = m.group(1).strip()
    else:
        # No code fence: drop any preamble before the first SELECT/WITH so a
        # sentence like "The answer is: SELECT ..." doesn't get scored as
        # invalid SQL purely because of the prefix.
        m = SQL_START_RE.search(text)
        if m:
            text = text[m.start() :]
    if ";" in text:
        text = text.split(";")[0] + ";"
    text = " ".join(text.split())  # collapse to one line
    if not text:
        # Empty generation: keep line counts aligned with gold file, and
        # let eval_sql.py score it as a (correct) failure.
        text = "SELECT 1=0"
    return text
