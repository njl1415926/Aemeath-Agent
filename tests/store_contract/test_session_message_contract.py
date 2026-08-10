from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier

import pytest

from app.session.errors import (
    SessionArchivedError,
    SessionEndedError,
    SessionOwnershipError,
    SessionTransitionError,
)
from app.session.pagination import parse_storage_datetime


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


def test_title_update_and_keyset_pagination_contract(store, monkeypatch):
    fixed = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc).isoformat()

    monkeypatch.setattr("app.db.sqlite_store.utc_now", lambda: fixed)
    monkeypatch.setattr("app.db.repositories.sessions.utc_now", lambda: fixed)
    monkeypatch.setattr("app.db.repositories.messages.utc_now", lambda: fixed)

    sessions = [
        store.create_session(user_id="user_1", character_id="aemeath", title=f"Session {i}")
        for i in range(4)
    ]
    archived = store.archive_session(session_id=sessions[0].id, user_id="user_1")
    with pytest.raises(SessionArchivedError):
        store.update_session_title(
            session_id=archived.id,
            user_id="user_1",
            title="cannot update archived",
        )

    updated = store.update_session_title(
        session_id=sessions[1].id,
        user_id="user_1",
        title="Updated",
    )
    assert updated.title == "Updated"
    with pytest.raises(SessionOwnershipError):
        store.update_session_title(
            session_id=sessions[1].id,
            user_id="user_2",
            title="wrong owner",
        )

    active_ids_desc = sorted([session.id for session in sessions[1:]], reverse=True)
    first_page = store.list_sessions_page(user_id="user_1", status="active", limit=2)
    assert [session.id for session in first_page] == active_ids_desc[:2]

    second_page = store.list_sessions_page(
        user_id="user_1",
        status="active",
        limit=2,
        after_updated_at=parse_storage_datetime(first_page[-1].updated_at),
        after_id=first_page[-1].id,
    )
    assert [session.id for session in second_page] == active_ids_desc[2:]


def test_message_keyset_pagination_uses_storage_time_boundary(store, monkeypatch):
    fixed = datetime(2026, 1, 1, 12, 30, tzinfo=timezone.utc).isoformat()

    monkeypatch.setattr("app.db.sqlite_store.utc_now", lambda: fixed)
    monkeypatch.setattr("app.db.repositories.messages.utc_now", lambda: fixed)

    session = store.create_session(user_id="user_1", character_id="aemeath")
    message_ids = [
        store.save_message(
            session_id=session.id,
            user_id="user_1",
            role="user",
            content=f"message {index}",
        )
        for index in range(4)
    ]

    expected_desc = sorted(message_ids, reverse=True)
    first_page = store.list_messages_by_session_page(session_id=session.id, limit=2)
    assert [message.id for message in first_page] == expected_desc[:2]

    second_page = store.list_messages_by_session_page(
        session_id=session.id,
        limit=2,
        before_created_at=parse_storage_datetime(first_page[-1].created_at),
        before_id=first_page[-1].id,
    )
    assert [message.id for message in second_page] == expected_desc[2:]


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


def test_concurrent_archive_cannot_be_revoked_by_end(store, monkeypatch):
    session = store.create_session(user_id="user_1", character_id="aemeath")
    _barrier_lifecycle_update_time(store, monkeypatch)

    with ThreadPoolExecutor(max_workers=2) as executor:
        end_future = executor.submit(
            store.end_session,
            session_id=session.id,
            user_id="user_1",
        )
        archive_future = executor.submit(
            store.archive_session,
            session_id=session.id,
            user_id="user_1",
        )
        outcomes = [_outcome(end_future), _outcome(archive_future)]

    final = store.get_session(session_id=session.id)
    assert final is not None
    assert final.status == "archived"
    assert any(outcome == "archived" for outcome in outcomes)


def test_concurrent_archive_cannot_be_revoked_by_resume(store, monkeypatch):
    session = store.create_session(user_id="user_1", character_id="aemeath")
    store.end_session(session_id=session.id, user_id="user_1")
    _barrier_lifecycle_update_time(store, monkeypatch)

    with ThreadPoolExecutor(max_workers=2) as executor:
        resume_future = executor.submit(
            store.resume_session,
            session_id=session.id,
            user_id="user_1",
            character_id="aemeath",
        )
        archive_future = executor.submit(
            store.archive_session,
            session_id=session.id,
            user_id="user_1",
        )
        outcomes = [_outcome(resume_future), _outcome(archive_future)]

    final = store.get_session(session_id=session.id)
    assert final is not None
    assert final.status == "archived"
    assert any(outcome == "archived" for outcome in outcomes)


def test_concurrent_end_does_not_return_active_when_resume_wins(store, monkeypatch):
    session = store.create_session(user_id="user_1", character_id="aemeath")
    store.end_session(session_id=session.id, user_id="user_1")
    _barrier_lifecycle_update_time(store, monkeypatch)

    with ThreadPoolExecutor(max_workers=2) as executor:
        end_future = executor.submit(
            store.end_session,
            session_id=session.id,
            user_id="user_1",
        )
        resume_future = executor.submit(
            store.resume_session,
            session_id=session.id,
            user_id="user_1",
            character_id="aemeath",
        )
        end_outcome = _outcome(end_future)
        resume_outcome = _outcome(resume_future)

    assert end_outcome != "active"
    assert resume_outcome in {"active", "transition"}


def _outcome(future) -> str:
    try:
        return future.result().status
    except SessionArchivedError:
        return "archived"
    except SessionTransitionError:
        return "transition"


def _barrier_lifecycle_update_time(store, monkeypatch) -> None:
    barrier = Barrier(2)
    timestamp = datetime(2026, 2, 1, 12, 0, tzinfo=timezone.utc).isoformat()

    def fake_utc_now():
        barrier.wait(timeout=5)
        return timestamp

    monkeypatch.setattr("app.db.sqlite_store.utc_now", fake_utc_now)
    monkeypatch.setattr("app.db.repositories.sessions.utc_now", fake_utc_now)
