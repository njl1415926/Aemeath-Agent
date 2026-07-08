from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.session.errors import (
    SessionArchivedError,
    SessionEndedError,
    SessionOwnershipError,
    SessionTransitionError,
)


def test_list_sessions_limit_returns_newest_updated_sessions(store, monkeypatch):
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    timestamps = iter(base + timedelta(seconds=offset) for offset in range(20))

    def fake_utc_now():
        return next(timestamps)

    monkeypatch.setattr("app.db.sqlite_store.utc_now", fake_utc_now)
    monkeypatch.setattr("app.db.repositories.sessions.utc_now", fake_utc_now)
    monkeypatch.setattr("app.db.repositories.messages.utc_now", fake_utc_now)

    oldest = store.create_session(user_id="user_1", character_id="aemeath", title="Oldest")
    middle = store.create_session(user_id="user_1", character_id="aemeath", title="Middle")
    newest = store.create_session(user_id="user_1", character_id="aemeath", title="Newest")

    store.save_message(
        session_id=oldest.id,
        user_id="user_1",
        role="user",
        content="oldest update",
    )
    store.save_message(
        session_id=middle.id,
        user_id="user_1",
        role="user",
        content="middle update",
    )
    store.save_message(
        session_id=newest.id,
        user_id="user_1",
        role="user",
        content="newest update",
    )

    sessions = store.list_sessions(user_id="user_1", status="active", limit=2)

    assert [session.id for session in sessions] == [newest.id, middle.id]
    assert sessions[0].updated_at > sessions[1].updated_at


def test_session_message_lifecycle_and_read_model_contract(store):
    first = store.create_session(
        user_id="user_1",
        character_id="aemeath",
        title="First",
        metadata={"client": "contract"},
    )
    second = store.create_session(user_id="user_1", character_id="other")
    store.create_session(user_id="user_2", character_id="aemeath")

    assert first.status == "active"
    assert first.metadata == {"client": "contract"}
    assert first.message_count == 0
    assert first.last_message_at is None

    user_message_id = store.save_message(
        session_id=first.id,
        user_id="user_1",
        role="user",
        content="hello",
    )
    assistant_message_id = store.save_message(
        session_id=first.id,
        user_id="user_1",
        role="assistant",
        content="hi",
    )
    assert store.get_message(message_id=user_message_id).content == "hello"
    assert store.get_message(message_id=assistant_message_id).role == "assistant"

    loaded = store.get_session(session_id=first.id)
    assert loaded is not None
    assert loaded.message_count == 2
    assert loaded.last_message_at is not None
    assert loaded.updated_at == loaded.last_message_at
    recent = store.get_recent_messages(session_id=first.id, limit=1)
    assert [message.content for message in recent] == ["hi"]
    assert [
        message.content for message in store.list_messages_by_session(session_id=first.id)
    ] == ["hello", "hi"]

    user_sessions = store.list_sessions(user_id="user_1", status=None)
    assert {session.id for session in user_sessions} == {first.id, second.id}
    character_sessions = store.list_sessions(
        user_id="user_1",
        character_id="aemeath",
        status="active",
    )
    assert [session.id for session in character_sessions] == [first.id]


def test_ensure_session_and_lifecycle_error_contract(store):
    created = store.ensure_session(
        session_id="opaque-session-id",
        user_id="user_1",
        character_id="aemeath",
        metadata={"client": "first"},
    )
    again = store.ensure_session(
        session_id=created.id,
        user_id="user_1",
        character_id="aemeath",
        metadata={"client": "ignored"},
    )
    assert again.metadata == {"client": "first"}

    with pytest.raises(SessionOwnershipError):
        store.ensure_session(
            session_id=created.id,
            user_id="user_2",
            character_id="aemeath",
        )
    with pytest.raises(SessionOwnershipError):
        store.require_session(
            session_id=created.id,
            user_id="user_1",
            character_id="other",
        )

    ended = store.end_session(session_id=created.id, user_id="user_1")
    ended_again = store.end_session(session_id=created.id, user_id="user_1")
    assert ended.status == "ended"
    assert ended.ended_at is not None
    assert ended_again.ended_at == ended.ended_at
    assert (
        store.require_session(
            session_id=created.id,
            user_id="user_1",
            character_id="aemeath",
        ).status
        == "ended"
    )
    with pytest.raises(SessionEndedError):
        store.ensure_session(
            session_id=created.id,
            user_id="user_1",
            character_id="aemeath",
        )

    resumed = store.resume_session(
        session_id=created.id,
        user_id="user_1",
        character_id="aemeath",
    )
    assert resumed.status == "active"
    assert resumed.ended_at is None
    with pytest.raises(SessionTransitionError):
        store.resume_session(
            session_id=created.id,
            user_id="user_1",
            character_id="aemeath",
        )

    store.archive_session(session_id=created.id, user_id="user_1")
    with pytest.raises(SessionArchivedError):
        store.require_session(
            session_id=created.id,
            user_id="user_1",
            character_id="aemeath",
        )
    with pytest.raises(SessionArchivedError):
        store.resume_session(
            session_id=created.id,
            user_id="user_1",
            character_id="aemeath",
        )
