from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from app.db.browser_auth_migrations import _extract_check_expressions, _normalize_sql
from app.db.errors import StorageMigrationRequiredError
from app.db.stream_turns import STREAM_TURN_TERMINAL_REASON_VALUES
from app.utils.ids import utc_now

STREAMING_CHAT_IDEMPOTENCY_V1_MIGRATION_ID = "streaming_chat_idempotency_v0_1"
STREAMING_CHAT_IDEMPOTENCY_MIGRATION_ID = "streaming_chat_idempotency_v0_2"
STREAMING_CHAT_IDEMPOTENCY_MIGRATION_IDS = (
    STREAMING_CHAT_IDEMPOTENCY_V1_MIGRATION_ID,
    STREAMING_CHAT_IDEMPOTENCY_MIGRATION_ID,
)
STREAMING_CHAT_IDEMPOTENCY_TABLE = "chat_request_idempotency"
_V1_TABLE = "chat_request_idempotency_v1_legacy"

_V1_TERMINAL_REASONS = (
    "cancelled",
    "client_disconnected",
    "consumer_slow",
    "model_failed",
    "model_invalid_response",
    "preparation_failed",
    "runtime_interrupted",
    "runtime_orphaned",
    "runtime_shutdown",
    "session_archived",
    "session_ended",
)


def _table_sql(*, terminal_reasons: tuple[str, ...]) -> str:
    terminal_reason_values = ",\n            ".join(
        f"'{reason}'" for reason in terminal_reasons
    )
    return f"""
CREATE TABLE {STREAMING_CHAT_IDEMPOTENCY_TABLE} (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    client_message_id TEXT NOT NULL,
    active_character_id TEXT NOT NULL,
    request_fingerprint_version INTEGER NOT NULL CHECK (request_fingerprint_version >= 1),
    request_fingerprint TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    user_message_id TEXT NOT NULL,
    assistant_message_id TEXT,
    status TEXT NOT NULL CHECK (status IN ('in_progress', 'completed', 'failed')),
    owner_runtime_id TEXT NOT NULL,
    warnings_json TEXT NOT NULL DEFAULT '[]',
    state_delta_json TEXT NOT NULL DEFAULT '{{}}',
    terminal_error_code TEXT,
    terminal_retryable INTEGER CHECK (terminal_retryable IN (0, 1) OR terminal_retryable IS NULL),
    terminal_reason TEXT CHECK (
        terminal_reason IS NULL OR terminal_reason IN (
            {terminal_reason_values}
        )
    ),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    failed_at TEXT,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE RESTRICT,
    FOREIGN KEY (user_message_id) REFERENCES messages(id) ON DELETE RESTRICT,
    FOREIGN KEY (assistant_message_id) REFERENCES messages(id) ON DELETE RESTRICT,
    CHECK (
        (status = 'in_progress' AND assistant_message_id IS NULL AND completed_at IS NULL
            AND failed_at IS NULL AND terminal_error_code IS NULL
            AND terminal_retryable IS NULL AND terminal_reason IS NULL
            AND warnings_json = '[]' AND state_delta_json = '{{}}')
        OR
        (status = 'completed' AND assistant_message_id IS NOT NULL AND completed_at IS NOT NULL
            AND failed_at IS NULL AND terminal_error_code IS NULL
            AND terminal_retryable IS NULL AND terminal_reason IS NULL)
        OR
        (status = 'failed' AND assistant_message_id IS NULL AND completed_at IS NULL
            AND failed_at IS NOT NULL AND terminal_error_code IS NOT NULL
            AND terminal_retryable IN (0, 1) AND terminal_reason IS NOT NULL
            AND warnings_json = '[]' AND state_delta_json = '{{}}')
    )
)
"""


_TABLE_SQL = _table_sql(terminal_reasons=STREAM_TURN_TERMINAL_REASON_VALUES)
_V1_TABLE_SQL = _table_sql(terminal_reasons=_V1_TERMINAL_REASONS)

_INDEX_SQL = """
CREATE UNIQUE INDEX uq_chat_request_idempotency_scope
    ON chat_request_idempotency (user_id, session_id, client_message_id);
CREATE UNIQUE INDEX uq_chat_request_idempotency_turn
    ON chat_request_idempotency (turn_id);
CREATE INDEX idx_chat_request_idempotency_orphan
    ON chat_request_idempotency (status, owner_runtime_id, updated_at, id);
"""

_REQUIRED_COLUMNS = {
    "id",
    "user_id",
    "session_id",
    "client_message_id",
    "active_character_id",
    "request_fingerprint_version",
    "request_fingerprint",
    "turn_id",
    "user_message_id",
    "assistant_message_id",
    "status",
    "owner_runtime_id",
    "warnings_json",
    "state_delta_json",
    "terminal_error_code",
    "terminal_retryable",
    "terminal_reason",
    "created_at",
    "updated_at",
    "completed_at",
    "failed_at",
}
_COPY_COLUMNS = tuple(sorted(_REQUIRED_COLUMNS))
_NULLABLE_COLUMNS = {
    "assistant_message_id",
    "terminal_error_code",
    "terminal_retryable",
    "terminal_reason",
    "completed_at",
    "failed_at",
}
_INTEGER_COLUMNS = {"request_fingerprint_version", "terminal_retryable"}
_DEFAULTS = {"warnings_json": "'[]'", "state_delta_json": "'{}'"}
_REQUIRED_FOREIGN_KEYS = {
    ("session_id", "sessions", "id", "RESTRICT"),
    ("user_message_id", "messages", "id", "RESTRICT"),
    ("assistant_message_id", "messages", "id", "RESTRICT"),
}
_REQUIRED_INDEXES = {
    "uq_chat_request_idempotency_scope": (True, ("user_id", "session_id", "client_message_id")),
    "uq_chat_request_idempotency_turn": (True, ("turn_id",)),
    "idx_chat_request_idempotency_orphan": (
        False,
        ("status", "owner_runtime_id", "updated_at", "id"),
    ),
}
_EXPECTED_CHECKS = _extract_check_expressions(_TABLE_SQL)
_V1_EXPECTED_CHECKS = _extract_check_expressions(_V1_TABLE_SQL)


def ensure_streaming_chat_schema(db_path: Path) -> None:
    """Create v0.2 or atomically promote the accepted v0.1 table."""

    with closing(_connect(db_path)) as connection:
        markers = _migration_markers(connection)
        table_exists = _table_exists(connection, STREAMING_CHAT_IDEMPOTENCY_TABLE)
        if STREAMING_CHAT_IDEMPOTENCY_MIGRATION_ID in markers:
            if not table_exists:
                raise StorageMigrationRequiredError(reason="streaming_chat_table_missing")
            _validate_schema(connection, expected_checks=_EXPECTED_CHECKS)
            _require_foreign_keys_clean(connection)
            return

        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            markers = _migration_markers(connection)
            table_exists = _table_exists(connection, STREAMING_CHAT_IDEMPOTENCY_TABLE)
            if STREAMING_CHAT_IDEMPOTENCY_MIGRATION_ID in markers:
                if not table_exists:
                    raise StorageMigrationRequiredError(reason="streaming_chat_table_missing")
            elif STREAMING_CHAT_IDEMPOTENCY_V1_MIGRATION_ID in markers:
                if not table_exists:
                    raise StorageMigrationRequiredError(reason="streaming_chat_table_missing")
                _validate_schema(connection, expected_checks=_V1_EXPECTED_CHECKS)
                _upgrade_v1_table(connection)
                _insert_v2_marker(connection)
            elif not table_exists:
                connection.execute(_TABLE_SQL)
                _execute_script(connection, _INDEX_SQL)
                _insert_v2_marker(connection)
            elif _schema_matches(connection, expected_checks=_V1_EXPECTED_CHECKS):
                _upgrade_v1_table(connection)
                _insert_v2_marker(connection)
            else:
                _validate_schema(connection, expected_checks=_EXPECTED_CHECKS)
                _insert_v2_marker(connection)

            _validate_schema(connection, expected_checks=_EXPECTED_CHECKS)
            _require_foreign_keys_clean(connection)
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        _validate_schema(connection, expected_checks=_EXPECTED_CHECKS)
        _require_foreign_keys_clean(connection)


def _upgrade_v1_table(connection: sqlite3.Connection) -> None:
    if _table_exists(connection, _V1_TABLE):
        raise StorageMigrationRequiredError(reason="streaming_chat_v1_upgrade_temp_exists")
    for name in _REQUIRED_INDEXES:
        connection.execute(f"DROP INDEX {name}")
    connection.execute(
        f"ALTER TABLE {STREAMING_CHAT_IDEMPOTENCY_TABLE} RENAME TO {_V1_TABLE}"
    )
    connection.execute(_TABLE_SQL)
    columns_sql = ", ".join(_COPY_COLUMNS)
    select_columns = ", ".join(
        (
            "CASE terminal_reason "
            "WHEN 'cancelled' THEN 'user_cancelled' "
            "WHEN 'runtime_shutdown' THEN 'runtime_closing' "
            "ELSE terminal_reason END AS terminal_reason"
            if column == "terminal_reason"
            else column
        )
        for column in _COPY_COLUMNS
    )
    try:
        connection.execute(
            f"INSERT INTO {STREAMING_CHAT_IDEMPOTENCY_TABLE} ({columns_sql}) "
            f"SELECT {select_columns} FROM {_V1_TABLE}"
        )
    except sqlite3.IntegrityError as exc:
        raise StorageMigrationRequiredError(
            reason="streaming_chat_v1_data_invalid"
        ) from exc
    connection.execute(f"DROP TABLE {_V1_TABLE}")
    _execute_script(connection, _INDEX_SQL)


def _insert_v2_marker(connection: sqlite3.Connection) -> None:
    connection.execute(
        "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
        (STREAMING_CHAT_IDEMPOTENCY_MIGRATION_ID, utc_now()),
    )


def _connect(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path, isolation_level=None, timeout=5.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        connection.close()
        raise StorageMigrationRequiredError(reason="foreign_keys_not_enabled")
    return connection


def _migration_markers(connection: sqlite3.Connection) -> set[str]:
    if not _table_exists(connection, "schema_migrations"):
        return set()
    placeholders = ", ".join("?" for _ in STREAMING_CHAT_IDEMPOTENCY_MIGRATION_IDS)
    rows = connection.execute(
        f"SELECT version FROM schema_migrations WHERE version IN ({placeholders})",
        STREAMING_CHAT_IDEMPOTENCY_MIGRATION_IDS,
    ).fetchall()
    return {str(row["version"]) for row in rows}


def _table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()
        is not None
    )


def _schema_matches(
    connection: sqlite3.Connection,
    *,
    expected_checks: tuple[str, ...],
) -> bool:
    try:
        _validate_schema(connection, expected_checks=expected_checks)
    except StorageMigrationRequiredError:
        return False
    return True


def _validate_schema(
    connection: sqlite3.Connection,
    *,
    expected_checks: tuple[str, ...],
) -> None:
    columns = {
        row["name"]: row
        for row in connection.execute(
            f"PRAGMA table_xinfo('{STREAMING_CHAT_IDEMPOTENCY_TABLE}')"
        ).fetchall()
    }
    if set(columns) != _REQUIRED_COLUMNS:
        raise StorageMigrationRequiredError(reason="streaming_chat_columns_invalid")
    for name, column in columns.items():
        expected_type = "INTEGER" if name in _INTEGER_COLUMNS else "TEXT"
        expected_default = _DEFAULTS.get(name)
        if (
            str(column["type"]).upper() != expected_type
            or int(column["hidden"]) != 0
            or column["dflt_value"] != expected_default
            or (name == "id") is not bool(column["pk"])
        ):
            raise StorageMigrationRequiredError(reason="streaming_chat_columns_invalid")
        if name != "id" and bool(column["notnull"]) is (name in _NULLABLE_COLUMNS):
            raise StorageMigrationRequiredError(reason="streaming_chat_columns_invalid")

    table_sql = connection.execute(
        "SELECT sql FROM sqlite_schema WHERE type = 'table' AND name = ?",
        (STREAMING_CHAT_IDEMPOTENCY_TABLE,),
    ).fetchone()
    if (
        table_sql is None
        or _extract_check_expressions(table_sql["sql"] or "") != expected_checks
    ):
        raise StorageMigrationRequiredError(reason="streaming_chat_check_invalid")

    foreign_keys = {
        (row["from"], row["table"], row["to"], row["on_delete"].upper())
        for row in connection.execute(
            f"PRAGMA foreign_key_list('{STREAMING_CHAT_IDEMPOTENCY_TABLE}')"
        ).fetchall()
    }
    if foreign_keys != _REQUIRED_FOREIGN_KEYS:
        raise StorageMigrationRequiredError(reason="streaming_chat_foreign_key_invalid")

    index_list = {
        row["name"]: row
        for row in connection.execute(
            f"PRAGMA index_list('{STREAMING_CHAT_IDEMPOTENCY_TABLE}')"
        ).fetchall()
    }
    for name, (unique, expected_columns) in _REQUIRED_INDEXES.items():
        index = index_list.get(name)
        if index is None or bool(index["unique"]) is not unique or bool(index["partial"]):
            raise StorageMigrationRequiredError(reason="streaming_chat_index_invalid")
        key_columns = tuple(
            row["name"]
            for row in connection.execute(f"PRAGMA index_xinfo('{name}')").fetchall()
            if int(row["key"]) == 1
        )
        if key_columns != expected_columns:
            raise StorageMigrationRequiredError(reason="streaming_chat_index_invalid")
        expected_sql = (
            "CREATE "
            + ("UNIQUE " if unique else "")
            + f"INDEX {name} ON {STREAMING_CHAT_IDEMPOTENCY_TABLE} "
            + f"({', '.join(expected_columns)})"
        )
        actual_sql = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE type = 'index' AND name = ?",
            (name,),
        ).fetchone()
        if _normalize_sql(actual_sql["sql"] if actual_sql else "") != _normalize_sql(
            expected_sql
        ):
            raise StorageMigrationRequiredError(reason="streaming_chat_index_invalid")


def _require_foreign_keys_clean(connection: sqlite3.Connection) -> None:
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise StorageMigrationRequiredError(reason="streaming_chat_foreign_key_violation")


def _execute_script(connection: sqlite3.Connection, script: str) -> None:
    for statement in script.split(";"):
        sql = statement.strip()
        if sql:
            connection.execute(sql)
