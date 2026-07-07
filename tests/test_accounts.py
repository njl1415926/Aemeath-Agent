from __future__ import annotations

import sqlite3
from pathlib import Path

from app.db.sqlite_store import SQLiteStore


def test_default_account_bootstrap(tmp_path: Path):
    store = SQLiteStore(tmp_path / "accounts.db")

    account = store.get_account(account_id="user_default")

    assert account is not None
    assert account.id == "user_default"
    assert account.username == "user_default"
    assert account.display_name == "Default User"
    assert account.status == "active"
    assert account.roles == ["user"]
    assert account.dev_token_hash is None


def test_account_backfill_collects_historical_user_ids_idempotently(tmp_path: Path):
    db_path = tmp_path / "accounts_backfill.db"
    store = SQLiteStore(db_path)
    store.create_session(user_id="session_user", character_id="aemeath")
    store.save_message(
        session_id="message_session",
        user_id="message_user",
        role="user",
        content="hello",
    )
    store.create_memory(
        user_id="memory_user",
        scope="user_global",
        scope_character_id=None,
        scope_session_id=None,
        namespace="user_profile",
        memory_type="preference",
        content="likes tea",
        importance=5,
        confidence=0.9,
        source="test",
        source_message_id=None,
        source_session_id=None,
        source_job_id=None,
        source_message_ids=[],
        related_memory_ids=[],
    )
    store.enqueue_memory_extraction_job(
        idempotency_key="job_key",
        user_id="job_user",
        session_id="job_session",
        character_id="aemeath",
        trigger_type="manual",
        source_message_ids=[],
        input_payload={},
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO relationship_states (
                id, user_id, character_id, updated_at
            )
            VALUES ('rel_1', 'relationship_user', 'aemeath', '2026-01-01T00:00:00+00:00')
            """
        )
        conn.execute("DELETE FROM accounts WHERE id != 'user_default'")

    reopened = SQLiteStore(db_path)
    again = SQLiteStore(db_path)

    expected_ids = {
        "user_default",
        "session_user",
        "message_user",
        "memory_user",
        "job_user",
        "relationship_user",
    }
    for account_id in expected_ids:
        account = reopened.get_account(account_id=account_id)
        assert account is not None
        assert account.username == account_id
        assert account.status == "active"

    with sqlite3.connect(db_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
    assert count == len(expected_ids)
    assert again.get_account(account_id="user_default") == reopened.get_account(
        account_id="user_default"
    )
