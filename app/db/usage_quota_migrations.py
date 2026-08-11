from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from app.db.browser_auth_migrations import _extract_check_expressions, _normalize_sql
from app.db.errors import StorageMigrationRequiredError
from app.utils.ids import utc_now

USAGE_QUOTA_MIGRATION_ID = "usage_quota_v0_1"
USAGE_QUOTA_DAILY_TABLE = "usage_quota_daily"
USAGE_QUOTA_RESERVATIONS_TABLE = "usage_quota_reservations"

_DAILY_TABLE_SQL = """
CREATE TABLE usage_quota_daily (
    user_id TEXT NOT NULL,
    quota_day TEXT NOT NULL,
    turn_limit INTEGER NOT NULL,
    token_limit INTEGER NOT NULL,
    estimator_version TEXT NOT NULL,
    reserved_turns INTEGER NOT NULL DEFAULT 0,
    settled_turns INTEGER NOT NULL DEFAULT 0,
    released_turns INTEGER NOT NULL DEFAULT 0,
    reserved_tokens INTEGER NOT NULL DEFAULT 0,
    settled_tokens INTEGER NOT NULL DEFAULT 0,
    released_tokens INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, quota_day),
    CHECK (turn_limit >= 1),
    CHECK (token_limit >= 1),
    CHECK (reserved_turns >= 0),
    CHECK (settled_turns >= 0),
    CHECK (released_turns >= 0),
    CHECK (reserved_tokens >= 0),
    CHECK (settled_tokens >= 0),
    CHECK (released_tokens >= 0),
    CHECK (reserved_turns + settled_turns <= turn_limit),
    CHECK (reserved_tokens + settled_tokens <= token_limit)
)
"""

_RESERVATIONS_TABLE_SQL = """
CREATE TABLE usage_quota_reservations (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    quota_day TEXT NOT NULL,
    surface TEXT NOT NULL CHECK (surface = 'stream_chat'),
    idempotency_key TEXT NOT NULL,
    session_id TEXT NOT NULL,
    character_id TEXT NOT NULL,
    stream_request_id TEXT NOT NULL,
    turn_id TEXT,
    owner_runtime_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('reserved', 'settled', 'released')),
    charging_phase TEXT NOT NULL CHECK (
        charging_phase IN ('reserved_pre_provider', 'provider_started', 'terminalized')
    ),
    reserved_turns INTEGER NOT NULL,
    reserved_tokens INTEGER NOT NULL,
    settled_turns INTEGER NOT NULL DEFAULT 0,
    settled_tokens INTEGER NOT NULL DEFAULT 0,
    estimator_version TEXT NOT NULL,
    context_budget_max_input_utf8_ceiling INTEGER NOT NULL,
    reserved_input_tokens INTEGER NOT NULL,
    reserved_output_tokens INTEGER NOT NULL,
    prompt_messages_utf8_bytes INTEGER NOT NULL DEFAULT 0,
    prompt_input_tokens INTEGER NOT NULL DEFAULT 0,
    output_utf8_bytes INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    delta_count INTEGER NOT NULL DEFAULT 0,
    terminal_error_code TEXT,
    terminal_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    provider_started_at TEXT,
    settled_at TEXT,
    released_at TEXT,
    UNIQUE (user_id, quota_day, idempotency_key),
    UNIQUE (stream_request_id),
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE RESTRICT,
    FOREIGN KEY (stream_request_id) REFERENCES chat_request_idempotency(id) ON DELETE RESTRICT,
    CHECK (reserved_turns >= 1),
    CHECK (reserved_tokens >= 0),
    CHECK (settled_turns >= 0),
    CHECK (settled_tokens >= 0),
    CHECK (context_budget_max_input_utf8_ceiling >= 0),
    CHECK (reserved_input_tokens >= 0),
    CHECK (reserved_output_tokens >= 0),
    CHECK (prompt_messages_utf8_bytes >= 0),
    CHECK (prompt_input_tokens >= 0),
    CHECK (output_utf8_bytes >= 0),
    CHECK (output_tokens >= 0),
    CHECK (delta_count >= 0),
    CHECK (
        (status = 'reserved' AND settled_turns = 0 AND settled_tokens = 0
            AND settled_at IS NULL AND released_at IS NULL)
        OR
        (status = 'settled' AND charging_phase = 'terminalized'
            AND settled_turns >= 1 AND settled_at IS NOT NULL AND released_at IS NULL)
        OR
        (status = 'released' AND charging_phase = 'terminalized'
            AND settled_turns = 0 AND settled_tokens = 0
            AND released_at IS NOT NULL AND settled_at IS NULL)
    )
)
"""

_INDEX_SQL = """
CREATE INDEX idx_usage_quota_reservations_user_day_status
    ON usage_quota_reservations (user_id, quota_day, status);
CREATE INDEX idx_usage_quota_reservations_turn
    ON usage_quota_reservations (turn_id);
CREATE INDEX idx_usage_quota_reservations_runtime_phase
    ON usage_quota_reservations (owner_runtime_id, charging_phase, status);
CREATE INDEX idx_usage_quota_reservations_stale_reserved
    ON usage_quota_reservations (status, owner_runtime_id, updated_at, id);
"""

_DAILY_REQUIRED_COLUMNS = {
    "user_id",
    "quota_day",
    "turn_limit",
    "token_limit",
    "estimator_version",
    "reserved_turns",
    "settled_turns",
    "released_turns",
    "reserved_tokens",
    "settled_tokens",
    "released_tokens",
    "created_at",
    "updated_at",
}
_DAILY_INTEGER_COLUMNS = {
    "turn_limit",
    "token_limit",
    "reserved_turns",
    "settled_turns",
    "released_turns",
    "reserved_tokens",
    "settled_tokens",
    "released_tokens",
}
_DAILY_DEFAULTS = {
    "reserved_turns": "0",
    "settled_turns": "0",
    "released_turns": "0",
    "reserved_tokens": "0",
    "settled_tokens": "0",
    "released_tokens": "0",
}
_DAILY_PK = {"user_id", "quota_day"}
_DAILY_CHECKS = _extract_check_expressions(_DAILY_TABLE_SQL)

_RESERVATION_REQUIRED_COLUMNS = {
    "id",
    "user_id",
    "quota_day",
    "surface",
    "idempotency_key",
    "session_id",
    "character_id",
    "stream_request_id",
    "turn_id",
    "owner_runtime_id",
    "status",
    "charging_phase",
    "reserved_turns",
    "reserved_tokens",
    "settled_turns",
    "settled_tokens",
    "estimator_version",
    "context_budget_max_input_utf8_ceiling",
    "reserved_input_tokens",
    "reserved_output_tokens",
    "prompt_messages_utf8_bytes",
    "prompt_input_tokens",
    "output_utf8_bytes",
    "output_tokens",
    "delta_count",
    "terminal_error_code",
    "terminal_reason",
    "created_at",
    "updated_at",
    "provider_started_at",
    "settled_at",
    "released_at",
}
_RESERVATION_INTEGER_COLUMNS = {
    "reserved_turns",
    "reserved_tokens",
    "settled_turns",
    "settled_tokens",
    "context_budget_max_input_utf8_ceiling",
    "reserved_input_tokens",
    "reserved_output_tokens",
    "prompt_messages_utf8_bytes",
    "prompt_input_tokens",
    "output_utf8_bytes",
    "output_tokens",
    "delta_count",
}
_RESERVATION_NULLABLE_COLUMNS = {
    "turn_id",
    "terminal_error_code",
    "terminal_reason",
    "provider_started_at",
    "settled_at",
    "released_at",
}
_RESERVATION_DEFAULTS = {
    "settled_turns": "0",
    "settled_tokens": "0",
    "prompt_messages_utf8_bytes": "0",
    "prompt_input_tokens": "0",
    "output_utf8_bytes": "0",
    "output_tokens": "0",
    "delta_count": "0",
}
_RESERVATION_FOREIGN_KEYS = {
    ("session_id", "sessions", "id", "RESTRICT"),
    ("stream_request_id", "chat_request_idempotency", "id", "RESTRICT"),
}
_RESERVATION_INDEXES = {
    "sqlite_autoindex_usage_quota_reservations_2": (
        True,
        ("user_id", "quota_day", "idempotency_key"),
    ),
    "sqlite_autoindex_usage_quota_reservations_3": (True, ("stream_request_id",)),
    "idx_usage_quota_reservations_user_day_status": (
        False,
        ("user_id", "quota_day", "status"),
    ),
    "idx_usage_quota_reservations_turn": (False, ("turn_id",)),
    "idx_usage_quota_reservations_runtime_phase": (
        False,
        ("owner_runtime_id", "charging_phase", "status"),
    ),
    "idx_usage_quota_reservations_stale_reserved": (
        False,
        ("status", "owner_runtime_id", "updated_at", "id"),
    ),
}
_RESERVATION_CHECKS = _extract_check_expressions(_RESERVATIONS_TABLE_SQL)


def ensure_usage_quota_schema(db_path: Path) -> None:
    with closing(_connect(db_path)) as connection:
        marker = _migration_marker_exists(connection)
        daily_exists = _table_exists(connection, USAGE_QUOTA_DAILY_TABLE)
        reservations_exists = _table_exists(connection, USAGE_QUOTA_RESERVATIONS_TABLE)
        if marker:
            if not daily_exists or not reservations_exists:
                raise StorageMigrationRequiredError(reason="usage_quota_table_missing")
            _validate_schema(connection)
            _require_foreign_keys_clean(connection)
            return

        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            if not _table_exists(connection, USAGE_QUOTA_DAILY_TABLE):
                connection.execute(_DAILY_TABLE_SQL)
            if not _table_exists(connection, USAGE_QUOTA_RESERVATIONS_TABLE):
                connection.execute(_RESERVATIONS_TABLE_SQL)
                _execute_script(connection, _INDEX_SQL)
            _validate_schema(connection)
            _require_foreign_keys_clean(connection)
            connection.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (USAGE_QUOTA_MIGRATION_ID, utc_now()),
            )
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        _validate_schema(connection)
        _require_foreign_keys_clean(connection)


def validate_usage_quota_schema_readonly(connection: sqlite3.Connection) -> None:
    if not _table_exists(connection, USAGE_QUOTA_DAILY_TABLE):
        raise StorageMigrationRequiredError(reason="usage_quota_daily_missing")
    if not _table_exists(connection, USAGE_QUOTA_RESERVATIONS_TABLE):
        raise StorageMigrationRequiredError(reason="usage_quota_reservations_missing")
    if not _migration_marker_exists(connection):
        raise StorageMigrationRequiredError(reason="usage_quota_marker_missing")
    _validate_schema(connection)
    _require_foreign_keys_clean(connection)


def _connect(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path, isolation_level=None, timeout=5.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        connection.close()
        raise StorageMigrationRequiredError(reason="foreign_keys_not_enabled")
    return connection


def _migration_marker_exists(connection: sqlite3.Connection) -> bool:
    if not _table_exists(connection, "schema_migrations"):
        return False
    return (
        connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version = ?",
            (USAGE_QUOTA_MIGRATION_ID,),
        ).fetchone()
        is not None
    )


def _table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()
        is not None
    )


def _validate_schema(connection: sqlite3.Connection) -> None:
    _validate_daily_schema(connection)
    _validate_reservation_schema(connection)


def _validate_daily_schema(connection: sqlite3.Connection) -> None:
    columns = _columns(connection, USAGE_QUOTA_DAILY_TABLE)
    if set(columns) != _DAILY_REQUIRED_COLUMNS:
        raise StorageMigrationRequiredError(reason="usage_quota_daily_columns_invalid")
    for name, column in columns.items():
        expected_type = "INTEGER" if name in _DAILY_INTEGER_COLUMNS else "TEXT"
        if (
            str(column["type"]).upper() != expected_type
            or int(column["hidden"]) != 0
            or column["dflt_value"] != _DAILY_DEFAULTS.get(name)
            or (name in _DAILY_PK) is not bool(column["pk"])
            or (name not in _DAILY_PK and not bool(column["notnull"]))
        ):
            raise StorageMigrationRequiredError(reason="usage_quota_daily_columns_invalid")
    if _table_checks(connection, USAGE_QUOTA_DAILY_TABLE) != _DAILY_CHECKS:
        raise StorageMigrationRequiredError(reason="usage_quota_daily_check_invalid")


def _validate_reservation_schema(connection: sqlite3.Connection) -> None:
    columns = _columns(connection, USAGE_QUOTA_RESERVATIONS_TABLE)
    if set(columns) != _RESERVATION_REQUIRED_COLUMNS:
        raise StorageMigrationRequiredError(reason="usage_quota_reservation_columns_invalid")
    for name, column in columns.items():
        expected_type = "INTEGER" if name in _RESERVATION_INTEGER_COLUMNS else "TEXT"
        expected_default = _RESERVATION_DEFAULTS.get(name)
        if (
            str(column["type"]).upper() != expected_type
            or int(column["hidden"]) != 0
            or column["dflt_value"] != expected_default
            or (name == "id") is not bool(column["pk"])
            or (name != "id" and bool(column["notnull"]) is (name in _RESERVATION_NULLABLE_COLUMNS))
        ):
            raise StorageMigrationRequiredError(
                reason="usage_quota_reservation_columns_invalid"
            )
    if _table_checks(connection, USAGE_QUOTA_RESERVATIONS_TABLE) != _RESERVATION_CHECKS:
        raise StorageMigrationRequiredError(reason="usage_quota_reservation_check_invalid")
    foreign_keys = {
        (row["from"], row["table"], row["to"], row["on_delete"].upper())
        for row in connection.execute(
            f"PRAGMA foreign_key_list('{USAGE_QUOTA_RESERVATIONS_TABLE}')"
        ).fetchall()
    }
    if foreign_keys != _RESERVATION_FOREIGN_KEYS:
        raise StorageMigrationRequiredError(reason="usage_quota_reservation_fk_invalid")
    _validate_reservation_indexes(connection)


def _validate_reservation_indexes(connection: sqlite3.Connection) -> None:
    index_list = {
        row["name"]: row
        for row in connection.execute(
            f"PRAGMA index_list('{USAGE_QUOTA_RESERVATIONS_TABLE}')"
        ).fetchall()
    }
    for name, (unique, expected_columns) in _RESERVATION_INDEXES.items():
        index = index_list.get(name)
        if index is None or bool(index["unique"]) is not unique or bool(index["partial"]):
            raise StorageMigrationRequiredError(reason="usage_quota_reservation_index_invalid")
        key_columns = tuple(
            row["name"]
            for row in connection.execute(f"PRAGMA index_xinfo('{name}')").fetchall()
            if int(row["key"]) == 1
        )
        if key_columns != expected_columns:
            raise StorageMigrationRequiredError(reason="usage_quota_reservation_index_invalid")
        if name.startswith("sqlite_autoindex_"):
            continue
        expected_sql = (
            "CREATE "
            + ("UNIQUE " if unique else "")
            + f"INDEX {name} ON {USAGE_QUOTA_RESERVATIONS_TABLE} "
            + f"({', '.join(expected_columns)})"
        )
        actual_sql = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE type = 'index' AND name = ?",
            (name,),
        ).fetchone()
        if _normalize_sql(actual_sql["sql"] if actual_sql else "") != _normalize_sql(
            expected_sql
        ):
            raise StorageMigrationRequiredError(reason="usage_quota_reservation_index_invalid")


def _columns(connection: sqlite3.Connection, table_name: str) -> dict[str, sqlite3.Row]:
    return {
        row["name"]: row
        for row in connection.execute(f"PRAGMA table_xinfo('{table_name}')").fetchall()
    }


def _table_checks(connection: sqlite3.Connection, table_name: str) -> tuple[str, ...]:
    row = connection.execute(
        "SELECT sql FROM sqlite_schema WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return _extract_check_expressions(row["sql"] if row else "")


def _require_foreign_keys_clean(connection: sqlite3.Connection) -> None:
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise StorageMigrationRequiredError(reason="usage_quota_foreign_key_violation")


def _execute_script(connection: sqlite3.Connection, script: str) -> None:
    for statement in script.split(";"):
        sql = statement.strip()
        if sql:
            connection.execute(sql)
