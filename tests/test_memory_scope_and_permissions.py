from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from app.db.sqlite_store import MEMORY_SCOPE_MIGRATION_VERSION, SQLiteStore
from app.main import build_controller
from app.memory.admin import MemoryAdminService
from app.memory.writer import QueuedMemoryWriter
from app.retrieval.embeddings import DeterministicHashEmbeddingProvider
from app.schemas import MemoryDecision
from app.session.errors import SessionArchivedError, SessionOwnershipError


def _create_memory(
    store: SQLiteStore,
    *,
    user_id: str = "user_1",
    content: str,
    scope: str = "user_global",
    scope_character_id: str | None = None,
    scope_session_id: str | None = None,
    source_session_id: str | None = None,
) -> str:
    return store.create_memory(
        user_id=user_id,
        scope=scope,
        scope_character_id=scope_character_id,
        scope_session_id=scope_session_id,
        namespace="user_profile",
        memory_type="preference",
        content=content,
        importance=8,
        confidence=0.9,
        source="test",
        source_message_id=None,
        source_session_id=source_session_id,
        source_job_id=None,
        source_message_ids=[],
        related_memory_ids=[],
    )


def test_require_session_accepts_active_and_ended_but_rejects_wrong_owner_and_archived(
    tmp_path: Path,
):
    store = SQLiteStore(tmp_path / "sessions.db")
    session = store.create_session(user_id="user_1", character_id="aemeath")

    assert (
        store.require_session(
            session_id=session.id,
            user_id="user_1",
            character_id="aemeath",
        ).status
        == "active"
    )
    store.end_session(session_id=session.id, user_id="user_1")
    assert (
        store.require_session(
            session_id=session.id,
            user_id="user_1",
            character_id="aemeath",
        ).status
        == "ended"
    )
    with pytest.raises(SessionOwnershipError):
        store.require_session(
            session_id=session.id,
            user_id="user_2",
            character_id="aemeath",
        )
    with pytest.raises(SessionOwnershipError):
        store.require_session(
            session_id=session.id,
            user_id="user_1",
            character_id="other",
        )

    store.archive_session(session_id=session.id, user_id="user_1")
    with pytest.raises(SessionArchivedError):
        store.require_session(
            session_id=session.id,
            user_id="user_1",
            character_id="aemeath",
        )


def test_memory_admin_validates_session_before_reading_messages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    store = SQLiteStore(tmp_path / "admin.db")
    session = store.create_session(user_id="user_1", character_id="aemeath")
    admin = MemoryAdminService(
        store,
        session_store=store,
        extraction_service=object(),  # type: ignore[arg-type]
    )

    def fail_if_read(*, session_id: str, limit: int):
        raise AssertionError(f"messages were read for {session_id=} {limit=}")

    monkeypatch.setattr(store, "get_recent_messages", fail_if_read)
    with pytest.raises(SessionOwnershipError):
        admin.extract_recent_session_memories(
            user_id="user_2",
            session_id=session.id,
            character_id="aemeath",
        )


def test_memory_admin_extract_accepts_ended_session_and_rejects_archived(settings):
    controller_settings = replace(settings, enable_long_term_memory=True)
    _controller, admin, resolved_settings, store = build_controller(
        settings=controller_settings
    )
    session = store.create_session(
        user_id=resolved_settings.default_user_id,
        character_id=resolved_settings.default_character_id,
    )
    store.save_message(
        session_id=session.id,
        user_id=resolved_settings.default_user_id,
        role="user",
        content="记住，我喜欢辣锅。",
    )
    store.end_session(session_id=session.id, user_id=resolved_settings.default_user_id)

    output = admin.handle_command(
        command="/memory extract 1",
        user_id=resolved_settings.default_user_id,
        session_id=session.id,
        character_id=resolved_settings.default_character_id,
    )
    assert "created" in output

    store.archive_session(
        session_id=session.id,
        user_id=resolved_settings.default_user_id,
    )
    with pytest.raises(SessionArchivedError):
        admin.handle_command(
            command="/memory extract 1",
            user_id=resolved_settings.default_user_id,
            session_id=session.id,
            character_id=resolved_settings.default_character_id,
        )


def test_memory_admin_delete_and_disable_are_owner_scoped(tmp_path: Path):
    store = SQLiteStore(tmp_path / "admin.db")
    first_id = _create_memory(store, content="owner-only archive")
    second_id = _create_memory(store, content="owner-only disable")
    admin = MemoryAdminService(store, session_store=store)

    assert (
        admin.archive_memory(memory_id=first_id, user_id="user_2")
        == "memory not found or unavailable"
    )
    assert (
        admin.disable_memory(memory_id=second_id, user_id="user_2")
        == "memory not found or unavailable"
    )
    assert store.get_memory(memory_id=first_id).status == "active"  # type: ignore[union-attr]
    assert store.get_memory(memory_id=second_id).is_active == 1  # type: ignore[union-attr]

    assert "archived memory" in admin.archive_memory(
        memory_id=first_id,
        user_id="user_1",
    )
    assert "disabled memory" in admin.disable_memory(
        memory_id=second_id,
        user_id="user_1",
    )


def test_queued_writer_rejects_source_message_context_mismatch(tmp_path: Path):
    store = SQLiteStore(tmp_path / "writer.db")
    session = store.create_session(user_id="user_1", character_id="aemeath")
    message_id = store.save_message(
        session_id=session.id,
        user_id="user_1",
        role="user",
        content="记住这件事",
    )
    writer = QueuedMemoryWriter(store)
    decision = MemoryDecision(
        should_write=True,
        operation="create",
        namespace="user_profile",
        memory_type="llm_extraction_job",
        content="记住这件事",
        importance=9,
        confidence=0.9,
        source="test",
    )

    with pytest.raises(ValueError, match="source message"):
        writer.persist_decision(
            user_id="user_2",
            decision=decision,
            source_message_id=message_id,
            session_id=session.id,
            character_id="aemeath",
        )
    with pytest.raises(ValueError, match="source message"):
        writer.persist_decision(
            user_id="user_1",
            decision=decision,
            source_message_id=message_id,
            session_id="other_session",
            character_id="aemeath",
        )
    with pytest.raises(ValueError, match="verified session_id and character_id"):
        writer.persist_decision(
            user_id="user_1",
            decision=decision,
            source_message_id=message_id,
            session_id="",
            character_id="aemeath",
        )
    with pytest.raises(ValueError, match="verified session_id and character_id"):
        writer.persist_decision(
            user_id="user_1",
            decision=decision,
            source_message_id=message_id,
            session_id=session.id,
            character_id="",
        )


def test_keyword_vector_and_fallback_share_scope_visibility(tmp_path: Path):
    store = SQLiteStore(tmp_path / "scope.db")
    global_id = _create_memory(store, content="scope-hit global")
    character_a_id = _create_memory(
        store,
        content="scope-hit character-a",
        scope="character",
        scope_character_id="aemeath",
    )
    character_b_id = _create_memory(
        store,
        content="scope-hit character-b",
        scope="character",
        scope_character_id="other",
    )
    session_a_id = _create_memory(
        store,
        content="scope-hit session-a",
        scope="session",
        scope_character_id="aemeath",
        scope_session_id="session_a",
    )
    session_b_id = _create_memory(
        store,
        content="scope-hit session-b",
        scope="session",
        scope_character_id="aemeath",
        scope_session_id="session_b",
    )

    visible_ids = {
        memory.id
        for memory in store.search_memories_keyword(
            user_id="user_1",
            character_id="aemeath",
            session_id="session_a",
            keywords=["scope-hit"],
        )
    }
    assert visible_ids == {global_id, character_a_id, session_a_id}
    fallback_ids = {
        memory.id
        for memory in store.list_recallable_memories(
            user_id="user_1",
            character_id="aemeath",
            session_id="session_a",
        )
    }
    assert fallback_ids == visible_ids

    embedder = DeterministicHashEmbeddingProvider()
    for memory_id in {
        global_id,
        character_a_id,
        character_b_id,
        session_a_id,
        session_b_id,
    }:
        store.upsert_memory_embedding(
            memory_id=memory_id,
            user_id="user_1",
            embedding_model=embedder.model_name,
            embedding=embedder.embed_query("scope-hit"),
        )
    vector_ids = {
        result.memory_id
        for result in store.search_memory_embeddings(
            user_id="user_1",
            character_id="aemeath",
            session_id="session_a",
            query_embedding=embedder.embed_query("scope-hit"),
            limit=10,
        )
    }
    assert vector_ids == visible_ids


def test_exact_dedupe_uses_target_scope_tuple(tmp_path: Path):
    store = SQLiteStore(tmp_path / "dedupe.db")
    content = "same-content"
    _create_memory(
        store,
        content=content,
        scope="session",
        scope_character_id="aemeath",
        scope_session_id="session_a",
    )

    assert not store.has_exact_active_memory(
        user_id="user_1",
        scope="user_global",
        scope_character_id=None,
        scope_session_id=None,
        content=content,
    )
    _create_memory(store, content=content)

    assert store.has_exact_active_memory(
        user_id="user_1",
        scope="user_global",
        scope_character_id=None,
        scope_session_id=None,
        content=content,
    )
    assert not store.has_exact_active_memory(
        user_id="user_1",
        scope="character",
        scope_character_id="aemeath",
        scope_session_id=None,
        content=content,
    )

    _create_memory(
        store,
        content=content,
        scope="character",
        scope_character_id="aemeath",
    )
    assert store.has_exact_active_memory(
        user_id="user_1",
        scope="character",
        scope_character_id="aemeath",
        scope_session_id=None,
        content=content,
    )
    assert not store.has_exact_active_memory(
        user_id="user_1",
        scope="character",
        scope_character_id="other",
        scope_session_id=None,
        content=content,
    )

    _create_memory(
        store,
        content=content,
        scope="character",
        scope_character_id="other",
    )
    assert store.has_exact_active_memory(
        user_id="user_1",
        scope="session",
        scope_character_id="aemeath",
        scope_session_id="session_a",
        content=content,
    )
    assert not store.has_exact_active_memory(
        user_id="user_1",
        scope="session",
        scope_character_id="aemeath",
        scope_session_id="session_b",
        content=content,
    )

    _create_memory(
        store,
        content=content,
        scope="session",
        scope_character_id="aemeath",
        scope_session_id="session_b",
    )
    assert len(store.list_active_memories(user_id="user_1")) == 5


def _create_legacy_memory_db(path: Path, *, related_memory_ids: str) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(
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
            );
            CREATE TABLE long_term_memories (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                namespace TEXT NOT NULL,
                memory_type TEXT NOT NULL,
                content TEXT NOT NULL,
                importance INTEGER NOT NULL,
                confidence REAL NOT NULL DEFAULT 1.0,
                source TEXT NOT NULL,
                source_message_id TEXT,
                related_memory_ids TEXT,
                version INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'active',
                last_used_at TEXT,
                use_count INTEGER NOT NULL DEFAULT 0,
                decay_score REAL,
                created_at TEXT NOT NULL,
                updated_at TEXT,
                expiry TEXT,
                is_active INTEGER DEFAULT 1
            );
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
            );
            """
        )
        conn.execute(
            """
            INSERT INTO messages (id, session_id, user_id, role, content, created_at)
            VALUES ('msg_1', 'session_1', 'user_1', 'user', 'remember this', '2026-01-01')
            """
        )
        for memory_id, related in [
            ("memory_1", related_memory_ids),
            ("memory_2", "[]"),
        ]:
            conn.execute(
                """
                INSERT INTO long_term_memories (
                    id, user_id, namespace, memory_type, content, importance,
                    confidence, source, source_message_id, related_memory_ids,
                    created_at
                )
                VALUES (?, 'user_1', 'user_profile', 'preference', ?, 8, 0.9,
                        'llm_memory_extraction', ?, ?, '2026-01-01')
                """,
                (
                    memory_id,
                    memory_id,
                    "msg_1" if memory_id == "memory_1" else None,
                    related,
                ),
            )
        conn.execute(
            """
            INSERT INTO memory_extraction_jobs (
                id, idempotency_key, user_id, session_id, character_id,
                trigger_type, status, source_message_ids_json, input_json,
                result_json, created_at
            )
            VALUES (
                'job_1', 'key_1', 'user_1', 'session_1', 'aemeath',
                'window', 'completed', '["msg_1"]', '{}',
                '{"created_memory_ids":["memory_1"]}', '2026-01-01'
            )
            """
        )


def test_memory_scope_migration_is_versioned_and_preserves_provenance(tmp_path: Path):
    db_path = tmp_path / "legacy.db"
    _create_legacy_memory_db(
        db_path,
        related_memory_ids='["msg_1", "memory_2", "unknown_legacy_ref"]',
    )

    with pytest.warns(RuntimeWarning, match="unknown_legacy_ref.*memory_1"):
        first_store = SQLiteStore(db_path)
    first = first_store.get_memory(memory_id="memory_1")
    second_store = SQLiteStore(db_path)
    second = second_store.get_memory(memory_id="memory_1")

    assert first == second
    assert first is not None
    assert first.scope == "user_global"
    assert first.scope_character_id is None
    assert first.scope_session_id is None
    assert first.source_session_id == "session_1"
    assert first.source_job_id == "job_1"
    assert first.source_message_ids == ["msg_1"]
    assert json.loads(first.related_memory_ids or "[]") == [
        "memory_2",
        "unknown_legacy_ref",
    ]
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = ?",
            (MEMORY_SCOPE_MIGRATION_VERSION,),
        ).fetchone()[0] == 1


def test_memory_scope_migration_rolls_back_on_invalid_legacy_json(tmp_path: Path):
    db_path = tmp_path / "broken.db"
    _create_legacy_memory_db(db_path, related_memory_ids="not-json")

    with pytest.raises(json.JSONDecodeError):
        SQLiteStore(db_path)

    with sqlite3.connect(db_path) as conn:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(long_term_memories)").fetchall()
        }
        assert "scope" not in columns
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = ?",
            (MEMORY_SCOPE_MIGRATION_VERSION,),
        ).fetchone()[0] == 0
