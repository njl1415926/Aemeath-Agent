from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.db.errors import StorageMigrationRequiredError
from app.db.sqlalchemy_store import SqlAlchemyStore
from app.db.sqlite_store import SQLiteStore
from app.db.streaming_chat_migrations import (
    _INDEX_SQL,
    _V1_TABLE_SQL,
    STREAMING_CHAT_IDEMPOTENCY_MIGRATION_ID,
    STREAMING_CHAT_IDEMPOTENCY_V1_MIGRATION_ID,
)
from app.db.usage_quota_migrations import USAGE_QUOTA_MIGRATION_ID


def _store(backend: str, db_path: Path):
    if backend == "sqlite_raw":
        return SQLiteStore(db_path)
    if backend == "sqlalchemy":
        return SqlAlchemyStore(db_path)
    raise AssertionError(f"unknown backend {backend!r}")


@pytest.mark.parametrize("backend", ["sqlite_raw", "sqlalchemy"])
def test_streaming_chat_migration_is_idempotent_and_preserves_existing_rows(
    tmp_path: Path,
    backend: str,
):
    db_path = tmp_path / f"{backend}-migration.db"
    first = _store(backend, db_path)
    session = first.create_session(user_id="user_1", character_id="aemeath")
    first.save_message(
        session_id=session.id,
        user_id="user_1",
        role="user",
        content="preexisting",
    )
    if isinstance(first, SqlAlchemyStore):
        first.engine.dispose()

    second = _store(backend, db_path)
    try:
        assert [
            item.content
            for item in second.list_messages_by_session(session_id=session.id)
        ] == ["preexisting"]
    finally:
        if isinstance(second, SqlAlchemyStore):
            second.engine.dispose()
    connection = sqlite3.connect(db_path)
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = ?",
            (STREAMING_CHAT_IDEMPOTENCY_MIGRATION_ID,),
        ).fetchone()[0] == 1
    finally:
        connection.close()


@pytest.mark.parametrize("backend", ["sqlite_raw", "sqlalchemy"])
@pytest.mark.parametrize(
    ("replacement_sql", "reason"),
    [
        (
            """
            CREATE UNIQUE INDEX uq_chat_request_idempotency_scope
            ON chat_request_idempotency
                (user_id, session_id, client_message_id, active_character_id)
            """,
            "streaming_chat_index_invalid",
        ),
        (
            """
            CREATE INDEX idx_chat_request_idempotency_orphan
            ON chat_request_idempotency (status, owner_runtime_id, id, updated_at)
            """,
            "streaming_chat_index_invalid",
        ),
    ],
)
def test_streaming_chat_schema_drift_fails_closed_on_restart(
    tmp_path: Path,
    backend: str,
    replacement_sql: str,
    reason: str,
):
    db_path = tmp_path / f"{backend}-drift.db"
    store = _store(backend, db_path)
    if isinstance(store, SqlAlchemyStore):
        store.engine.dispose()
    index_name = (
        "uq_chat_request_idempotency_scope"
        if "uq_chat_request_idempotency_scope" in replacement_sql
        else "idx_chat_request_idempotency_orphan"
    )
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(f"DROP INDEX {index_name}")
        connection.execute(replacement_sql)
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(StorageMigrationRequiredError) as raised:
        _store(backend, db_path)
    assert raised.value.metadata["reason"] == reason


@pytest.mark.parametrize("backend", ["sqlite_raw", "sqlalchemy"])
def test_streaming_chat_missing_table_with_marker_fails_closed(tmp_path: Path, backend: str):
    db_path = tmp_path / f"{backend}-missing-table.db"
    store = _store(backend, db_path)
    if isinstance(store, SqlAlchemyStore):
        store.engine.dispose()
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("DROP TABLE chat_request_idempotency")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(StorageMigrationRequiredError) as raised:
        _store(backend, db_path)
    assert raised.value.metadata["reason"] == "streaming_chat_table_missing"


@pytest.mark.parametrize("backend", ["sqlite_raw", "sqlalchemy"])
def test_streaming_chat_v1_table_is_promoted_atomically(tmp_path: Path, backend: str):
    db_path = tmp_path / f"{backend}-v1-upgrade.db"
    first = _store(backend, db_path)
    session = first.create_session(user_id="user_1", character_id="aemeath")
    message = first.save_message(
        session_id=session.id,
        user_id="user_1",
        role="user",
        content="v1 request",
    )
    if isinstance(first, SqlAlchemyStore):
        first.engine.dispose()

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "DELETE FROM schema_migrations WHERE version = ?",
            (USAGE_QUOTA_MIGRATION_ID,),
        )
        connection.execute("DROP TABLE usage_quota_reservations")
        connection.execute("DROP TABLE usage_quota_daily")
        for index_name in (
            "uq_chat_request_idempotency_scope",
            "uq_chat_request_idempotency_turn",
            "idx_chat_request_idempotency_orphan",
        ):
            connection.execute(f"DROP INDEX {index_name}")
        connection.execute(
            "ALTER TABLE chat_request_idempotency "
            "RENAME TO chat_request_idempotency_v2_source"
        )
        connection.execute(_V1_TABLE_SQL)
        connection.executescript(_INDEX_SQL)
        connection.execute(
            """
            INSERT INTO chat_request_idempotency (
                id, user_id, session_id, client_message_id, active_character_id,
                request_fingerprint_version, request_fingerprint, turn_id, user_message_id,
                assistant_message_id, status, owner_runtime_id, warnings_json, state_delta_json,
                terminal_error_code, terminal_retryable, terminal_reason, created_at, updated_at,
                completed_at, failed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 'failed', ?, '[]', '{}', ?, 1, ?, ?, ?,
                      NULL, ?)
            """,
            (
                "stream_request_v1",
                "user_1",
                session.id,
                "cm-v1",
                "aemeath",
                1,
                "opaque-v1",
                "turn-v1",
                message,
                "runtime_v1",
                "MODEL_CANCELLED",
                "cancelled",
                "2026-08-05T00:00:00+00:00",
                "2026-08-05T00:00:00+00:00",
                "2026-08-05T00:00:00+00:00",
            ),
        )
        connection.execute("DROP TABLE chat_request_idempotency_v2_source")
        connection.execute(
            "DELETE FROM schema_migrations WHERE version = ?",
            (STREAMING_CHAT_IDEMPOTENCY_MIGRATION_ID,),
        )
        connection.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (STREAMING_CHAT_IDEMPOTENCY_V1_MIGRATION_ID, "2026-08-05T00:00:00+00:00"),
        )

    upgraded = _store(backend, db_path)
    try:
        with sqlite3.connect(db_path) as connection:
            assert connection.execute(
                "SELECT terminal_reason FROM chat_request_idempotency WHERE id = ?",
                ("stream_request_v1",),
            ).fetchone()[0] == "user_cancelled"
            assert connection.execute(
                "SELECT COUNT(*) FROM schema_migrations WHERE version = ?",
                (STREAMING_CHAT_IDEMPOTENCY_MIGRATION_ID,),
            ).fetchone()[0] == 1
    finally:
        if isinstance(upgraded, SqlAlchemyStore):
            upgraded.engine.dispose()
