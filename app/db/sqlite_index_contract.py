from __future__ import annotations

from collections.abc import Sequence
from typing import Any

ACTIVE_MEMORY_IDENTITY_INDEX = "uq_memories_active_exact_identity"
_ACTIVE_MEMORY_IDENTITY_COLUMNS = (
    "user_id,scope,coalesce(scope_character_id,''),"
    "coalesce(scope_session_id,''),content"
)
_ACTIVE_MEMORY_IDENTITY_WHERE = "status='active'andis_active=1"


def _normalize_sql(sql: str) -> str:
    """Normalize SQL structure without changing single-quoted literal bytes."""

    normalized: list[str] = []
    in_single_quote = False
    position = 0
    while position < len(sql):
        token = sql[position]
        if in_single_quote:
            normalized.append(token)
            if token == "'":
                if position + 1 < len(sql) and sql[position + 1] == "'":
                    normalized.append("'")
                    position += 2
                    continue
                in_single_quote = False
            position += 1
            continue
        if token == "'":
            normalized.append(token)
            in_single_quote = True
        elif token.isspace() or token in {'"', "`"}:
            pass
        else:
            normalized.append(token.lower())
        position += 1
    return "".join(normalized).rstrip(";")


def _split_parenthesized(sql: str, *, start: int) -> tuple[str, str] | None:
    if start >= len(sql) or sql[start] != "(":
        return None
    depth = 0
    in_single_quote = False
    position = start
    while position < len(sql):
        token = sql[position]
        if in_single_quote:
            if token == "'":
                if position + 1 < len(sql) and sql[position + 1] == "'":
                    position += 2
                    continue
                in_single_quote = False
            position += 1
            continue
        if token == "'":
            in_single_quote = True
        elif token == "(":
            depth += 1
        elif token == ")":
            depth -= 1
            if depth == 0:
                return sql[start + 1 : position], sql[position + 1 :]
        position += 1
    return None


def has_valid_active_memory_identity_index(
    *,
    index_list_rows: Sequence[Any],
    index_xinfo_rows: Sequence[Any],
    index_sql: object,
) -> bool:
    """Validate the exact SQLite partial expression-index contract.

    SQLite reflection cannot fully represent expression indexes. The index-list
    flags, index-xinfo key layout, and persisted DDL are all required so an
    expanded or narrowed partial predicate cannot be accepted by accident.
    """

    index = next(
        (row for row in index_list_rows if row["name"] == ACTIVE_MEMORY_IDENTITY_INDEX),
        None,
    )
    if index is None or index["unique"] != 1 or index["partial"] != 1:
        return False

    key_parts = sorted(
        (row for row in index_xinfo_rows if row["key"] == 1),
        key=lambda row: row["seqno"],
    )
    expected_names = ("user_id", "scope", None, None, "content")
    if len(key_parts) != len(expected_names):
        return False
    for sequence, (part, expected_name) in enumerate(
        zip(key_parts, expected_names, strict=True)
    ):
        if part["seqno"] != sequence or part["desc"] != 0:
            return False
        if expected_name is None:
            if part["cid"] != -2 or part["name"] is not None:
                return False
        elif part["name"] != expected_name or part["cid"] < 0:
            return False

    if not isinstance(index_sql, str):
        return False
    normalized = _normalize_sql(index_sql)
    prefix = f"createuniqueindex{ACTIVE_MEMORY_IDENTITY_INDEX}onlong_term_memories"
    if not normalized.startswith(prefix):
        return False
    definition = _split_parenthesized(normalized, start=len(prefix))
    if definition is None:
        return False
    columns, suffix = definition
    return (
        columns == _ACTIVE_MEMORY_IDENTITY_COLUMNS
        and suffix == f"where{_ACTIVE_MEMORY_IDENTITY_WHERE}"
    )
