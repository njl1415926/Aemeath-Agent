from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.db.sqlite_store import SQLiteStore
from app.session.errors import (
    SessionArchivedError,
    SessionEndedError,
    SessionMigrationError,
    SessionOwnershipError,
    SessionTransitionError,
)


def test_session_create_get_list_and_read_model(tmp_path: Path):
    store = SQLiteStore(tmp_path / "sessions.db")
    first = store.create_session(
        user_id="user_1",
        character_id="aemeath",
        title="First",
        metadata={"client": "test"},
    )
    second = store.create_session(user_id="user_1", character_id="other")
    store.create_session(user_id="user_2", character_id="aemeath")

    assert first.status == "active"
    assert first.metadata == {"client": "test"}
    assert first.message_count == 0
    assert first.last_message_at is None

    store.save_message(
        session_id=first.id,
        user_id="user_1",
        role="user",
        content="hello",
    )
    store.save_message(
        session_id=first.id,
        user_id="user_1",
        role="assistant",
        content="hi",
    )

    loaded = store.get_session(session_id=first.id)
    assert loaded is not None
    assert loaded.message_count == 2
    assert loaded.last_message_at is not None
    assert loaded.updated_at == loaded.last_message_at

    user_sessions = store.list_sessions(user_id="user_1", status=None)
    assert {session.id for session in user_sessions} == {first.id, second.id}
    character_sessions = store.list_sessions(
        user_id="user_1",
        character_id="aemeath",
        status="active",
    )
    assert [session.id for session in character_sessions] == [first.id]


def test_ensure_session_is_idempotent_and_rejects_ownership(tmp_path: Path):
    store = SQLiteStore(tmp_path / "sessions.db")
    first = store.ensure_session(
        session_id="opaque-session-id",
        user_id="user_1",
        character_id="aemeath",
        metadata={"client": "first"},
    )
    second = store.ensure_session(
        session_id="opaque-session-id",
        user_id="user_1",
        character_id="aemeath",
        metadata={"client": "ignored"},
    )

    assert first == second
    assert second.metadata == {"client": "first"}

    with pytest.raises(SessionOwnershipError):
        store.ensure_session(
            session_id=first.id,
            user_id="user_2",
            character_id="aemeath",
        )
    with pytest.raises(SessionOwnershipError):
        store.ensure_session(
            session_id=first.id,
            user_id="user_1",
            character_id="other",
        )


def test_session_lifecycle_transitions(tmp_path: Path):
    store = SQLiteStore(tmp_path / "sessions.db")
    session = store.create_session(user_id="user_1", character_id="aemeath")

    ended = store.end_session(session_id=session.id, user_id="user_1")
    ended_again = store.end_session(session_id=session.id, user_id="user_1")
    assert ended.status == "ended"
    assert ended.ended_at is not None
    assert ended_again.ended_at == ended.ended_at
    with pytest.raises(SessionEndedError):
        store.ensure_session(
            session_id=session.id,
            user_id="user_1",
            character_id="aemeath",
        )

    resumed = store.resume_session(
        session_id=session.id,
        user_id="user_1",
        character_id="aemeath",
    )
    assert resumed.status == "active"
    assert resumed.ended_at is None
    with pytest.raises(SessionTransitionError):
        store.resume_session(
            session_id=session.id,
            user_id="user_1",
            character_id="aemeath",
        )

    ended = store.end_session(session_id=session.id, user_id="user_1")
    archived = store.archive_session(session_id=session.id, user_id="user_1")
    archived_again = store.archive_session(session_id=session.id, user_id="user_1")
    assert archived.status == "archived"
    assert archived.ended_at == ended.ended_at
    assert archived.archived_at is not None
    assert archived_again.archived_at == archived.archived_at
    with pytest.raises(SessionArchivedError):
        store.ensure_session(
            session_id=session.id,
            user_id="user_1",
            character_id="aemeath",
        )
    with pytest.raises(SessionArchivedError):
        store.resume_session(
            session_id=session.id,
            user_id="user_1",
            character_id="aemeath",
        )


def _create_legacy_messages_db(path: Path, rows: list[tuple[str, str, str, str, str, str]]) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE messages (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL,
                emotion_label TEXT,
                importance_score INTEGER
            )
            """
        )
        conn.executemany(
            """
            INSERT INTO messages (
                id, session_id, user_id, role, content, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            rows,
        )


def test_legacy_message_backfill_is_idempotent_and_preserves_messages(tmp_path: Path):
    db_path = tmp_path / "legacy.db"
    legacy_rows = [
        ("msg_1", "legacy_session", "user_1", "user", "hello", "2026-01-01T00:00:00+00:00"),
        (
            "msg_2",
            "legacy_session",
            "user_1",
            "assistant",
            "hi",
            "2026-01-01T00:01:00+00:00",
        ),
    ]
    _create_legacy_messages_db(db_path, legacy_rows)

    first_store = SQLiteStore(db_path)
    first = first_store.get_session(session_id="legacy_session")
    second_store = SQLiteStore(db_path)
    second = second_store.get_session(session_id="legacy_session")

    assert first == second
    assert first is not None
    assert first.user_id == "user_1"
    assert first.character_id == "aemeath"
    assert first.created_at == "2026-01-01T00:00:00+00:00"
    assert first.last_message_at == "2026-01-01T00:01:00+00:00"
    assert first.message_count == 2
    assert [
        (message.id, message.content, message.created_at)
        for message in second_store.list_messages_by_session(session_id="legacy_session")
    ] == [
        ("msg_1", "hello", "2026-01-01T00:00:00+00:00"),
        ("msg_2", "hi", "2026-01-01T00:01:00+00:00"),
    ]


def test_legacy_mixed_owner_backfill_rolls_back_without_losing_messages(tmp_path: Path):
    db_path = tmp_path / "conflict.db"
    _create_legacy_messages_db(
        db_path,
        [
            ("msg_1", "shared", "user_1", "user", "one", "2026-01-01T00:00:00+00:00"),
            ("msg_2", "shared", "user_2", "user", "two", "2026-01-01T00:01:00+00:00"),
        ],
    )

    with pytest.raises(SessionMigrationError):
        SQLiteStore(db_path)

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


def test_legacy_memory_job_without_messages_backfills_session(tmp_path: Path):
    db_path = tmp_path / "jobs_only.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE memory_extraction_jobs (
                id TEXT PRIMARY KEY,
                idempotency_key TEXT NOT NULL UNIQUE,
                user_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                character_id TEXT NOT NULL,
                trigger_type TEXT NOT NULL,
                status TEXT NOT NULL,
                source_message_ids_json TEXT NOT NULL,
                input_json TEXT NOT NULL,
                result_json TEXT,
                error TEXT,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                locked_by TEXT,
                locked_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO memory_extraction_jobs (
                id, idempotency_key, user_id, session_id, character_id,
                trigger_type, status, source_message_ids_json, input_json,
                created_at
            )
            VALUES (
                'job_1', 'key_1', 'user_1', 'job_session', 'aemeath',
                'hard', 'pending', '[]', '{}', '2026-01-01T00:00:00+00:00'
            )
            """
        )

    store = SQLiteStore(db_path)
    session = store.get_session(session_id="job_session")

    assert session is not None
    assert (session.user_id, session.character_id) == ("user_1", "aemeath")
    assert session.message_count == 0
    assert session.last_message_at is None
    [job] = store.list_memory_extraction_jobs(session_id="job_session")
    assert (job.id, job.user_id, job.session_id, job.character_id) == (
        "job_1",
        "user_1",
        "job_session",
        "aemeath",
    )


def test_non_default_character_session_survives_restart(tmp_path: Path):
    db_path = tmp_path / "restart.db"
    store = SQLiteStore(db_path)
    session = store.create_session(
        user_id="user_1",
        character_id="other_character",
    )
    store.save_message(
        session_id=session.id,
        user_id="user_1",
        role="user",
        content="hello",
    )
    before = store.get_session(session_id=session.id)
    messages_before = store.list_messages_by_session(session_id=session.id)

    reopened = SQLiteStore(db_path)
    after = reopened.get_session(session_id=session.id)
    messages_after = reopened.list_messages_by_session(session_id=session.id)

    assert after == before
    assert after is not None
    assert after.character_id == "other_character"
    assert after.message_count == 1
    assert after.last_message_at == messages_before[0].created_at
    assert messages_after == messages_before


def test_legacy_jobs_with_multiple_owners_are_rejected(tmp_path: Path):
    db_path = tmp_path / "job_conflict.db"
    store = SQLiteStore(db_path)
    store.enqueue_memory_extraction_job(
        idempotency_key="key_1",
        user_id="user_1",
        session_id="shared_session",
        character_id="aemeath",
        trigger_type="hard",
        source_message_ids=[],
        input_payload={},
    )
    store.enqueue_memory_extraction_job(
        idempotency_key="key_2",
        user_id="user_2",
        session_id="shared_session",
        character_id="other_character",
        trigger_type="hard",
        source_message_ids=[],
        input_payload={},
    )

    with pytest.raises(SessionMigrationError):
        SQLiteStore(db_path)

    assert len(store.list_memory_extraction_jobs(session_id="shared_session")) == 2
    assert store.get_session(session_id="shared_session") is None


def test_memory_job_conflicting_with_existing_session_is_rejected(tmp_path: Path):
    db_path = tmp_path / "existing_job_conflict.db"
    store = SQLiteStore(db_path)
    session = store.create_session(
        user_id="user_1",
        character_id="other_character",
    )
    store.enqueue_memory_extraction_job(
        idempotency_key="key_1",
        user_id="user_1",
        session_id=session.id,
        character_id="aemeath",
        trigger_type="hard",
        source_message_ids=[],
        input_payload={},
    )

    with pytest.raises(SessionMigrationError):
        SQLiteStore(db_path)

    unchanged = store.get_session(session_id=session.id)
    assert unchanged is not None
    assert unchanged.character_id == "other_character"
    [job] = store.list_memory_extraction_jobs(session_id=session.id)
    assert job.character_id == "aemeath"


def test_message_owner_conflicting_with_existing_session_is_rejected(tmp_path: Path):
    db_path = tmp_path / "existing_message_conflict.db"
    store = SQLiteStore(db_path)
    session = store.create_session(
        user_id="user_1",
        character_id="aemeath",
    )
    store.save_message(
        session_id=session.id,
        user_id="user_2",
        role="user",
        content="wrong owner",
    )

    with pytest.raises(SessionMigrationError):
        SQLiteStore(db_path)

    [message] = store.list_messages_by_session(session_id=session.id)
    assert message.user_id == "user_2"
    unchanged = store.get_session(session_id=session.id)
    assert unchanged is not None
    assert unchanged.user_id == "user_1"
