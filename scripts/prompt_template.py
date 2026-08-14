from typing import Dict, List

SYSTEM_PROMPT = (
    "You are a text-to-SQL model. Given a database schema and a question, "
    "write a single SQLite SQL query that answers the question.\n"
    "Rules:\n"
    "- Output only the SQL query, nothing else: no explanation, no markdown "
    "code fences, no comments.\n"
    "- Use only the tables and columns given in the schema.\n"
    "- End the query with a single semicolon."
)

USER_TEMPLATE = (
    "### Database schema\n"
    "{schema}\n\n"
    "### Question\n"
    "{question}\n\n"
    "### SQL query"
)


def build_messages(question: str, schema_str: str) -> List[Dict[str, str]]:
    """Build a chat-format message list for tokenizer.apply_chat_template."""
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": USER_TEMPLATE.format(schema=schema_str, question=question)},
    ]
