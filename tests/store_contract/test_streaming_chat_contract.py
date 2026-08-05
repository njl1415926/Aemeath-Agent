from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from app.db.errors import StorageMigrationRequiredError
from app.db.sqlalchemy_store import SqlAlchemyStore
from app.db.sqlite_store import SQLiteStore
from app.db.stream_turns import (
    STREAM_TURN_TERMINAL_REASON_VALUES,
    StreamTurnComplete,
    StreamTurnFail,
    StreamTurnResolveOrReserve,
)
from app.schemas import StateUpdate
from app.session.errors import SessionArchivedError, SessionEndedError, SessionOwnershipError


def _resolve_request(
    *,
    session_id: str,
    client_message_id: str,
    fingerprint: str = "opaque-fingerprint-v1",
    owner_runtime_id: str = "runtime_1",
    turn_id: str | None = None,
    user_message_id: str | None = None,
    message: str = "hello",
    user_id: str = "user_1",
    active_character_id: str = "aemeath",
) -> StreamTurnResolveOrReserve:
    suffix = client_message_id.replace("-", "_")
    return StreamTurnResolveOrReserve(
        user_id=user_id,
        session_id=session_id,
        active_character_id=active_character_id,
        client_message_id=client_message_id,
        request_fingerprint_version=1,
        request_fingerprint=fingerprint,
        owner_runtime_id=owner_runtime_id,
        turn_id=turn_id or f"turn_{suffix}",
        user_message_id=user_message_id or f"message_{suffix}",
        user_message_content=message,
    )


def _state_update(*, trust: int = 2, affection: int = 3, familiarity: int = 4) -> StateUpdate:
    return StateUpdate(
        user_emotion="glad",
        aemeath_mood="warm",
        trust_delta=trust,
        affection_delta=affection,
        familiarity_delta=familiarity,
        last_topic="streaming contract",
        reason="test",
    )


def _complete_request(
    record,
    *,
    response: str = "durable assistant",
    update: StateUpdate | None = None,
):
    return StreamTurnComplete(
        record_id=record.id,
        turn_id=record.turn_id,
        user_id=record.user_id,
        session_id=record.session_id,
        owner_runtime_id=record.owner_runtime_id,
        assistant_response=response,
        state_update=update or _state_update(),
        warnings=("model_output_truncated",),
    )


def _fail_request(record, *, reason: str = "model_failed") -> StreamTurnFail:
    return StreamTurnFail(
        record_id=record.id,
        turn_id=record.turn_id,
        user_id=record.user_id,
        session_id=record.session_id,
        owner_runtime_id=record.owner_runtime_id,
        terminal_error_code="MODEL_TIMEOUT",
        terminal_retryable=True,
        terminal_reason=reason,
    )


def _make_store(backend: str, db_path: Path):
    if backend == "sqlite_raw":
        return SQLiteStore(db_path)
    if backend == "sqlalchemy":
        return SqlAlchemyStore(db_path)
    raise AssertionError(f"unknown backend {backend!r}")


def test_stream_turn_exactly_once_complete_and_replay(store):
    session = store.create_session(user_id="user_1", character_id="aemeath")
    first = store.resolve_or_reserve(
        request=_resolve_request(session_id=session.id, client_message_id="cm-complete")
    )
    duplicate = store.resolve_or_reserve(
        request=_resolve_request(
            session_id=session.id,
            client_message_id="cm-complete",
            turn_id="turn_duplicate",
            user_message_id="message_duplicate",
        )
    )

    assert first.kind == "reserved"
    assert first.record.active_character_id == "aemeath"
    assert duplicate.kind == "in_progress"
    assert duplicate.record.id == first.record.id
    assert [message.role for message in store.list_messages_by_session(session_id=session.id)] == [
        "user"
    ]

    completed = store.complete(request=_complete_request(first.record))
    assert completed.status == "completed"
    assert completed.assistant_message_id is not None
    assert completed.warnings == ("model_output_truncated",)
    assert completed.state_delta == {"affection": 3, "familiarity": 4, "trust": 2}
    assert store.complete(request=_complete_request(first.record)).id == completed.id

    replay = store.resolve_or_reserve(
        request=_resolve_request(
            session_id=session.id,
            client_message_id="cm-complete",
            turn_id="turn_replay",
            user_message_id="message_replay",
        )
    )
    assert replay.kind == "replay_completed"
    assert replay.assistant_message is not None
    assert replay.assistant_message.content == "durable assistant"
    assert [message.role for message in store.list_messages_by_session(session_id=session.id)] == [
        "user",
        "assistant",
    ]


def test_stream_turn_fail_is_terminal_without_partial_assistant(store):
    session = store.create_session(user_id="user_1", character_id="aemeath")
    reserved = store.resolve_or_reserve(
        request=_resolve_request(session_id=session.id, client_message_id="cm-failed")
    )
    failed = store.fail(request=_fail_request(reserved.record))

    assert failed.status == "failed"
    assert failed.assistant_message_id is None
    assert failed.terminal_error_code == "MODEL_TIMEOUT"
    assert store.fail(request=_fail_request(reserved.record)).status == "failed"
    assert [message.role for message in store.list_messages_by_session(session_id=session.id)] == [
        "user"
    ]
    replay = store.resolve_or_reserve(
        request=_resolve_request(
            session_id=session.id,
            client_message_id="cm-failed",
            turn_id="turn-failed-replay",
            user_message_id="message-failed-replay",
        )
    )
    assert replay.kind == "replay_failed"


@pytest.mark.parametrize("reason", STREAM_TURN_TERMINAL_REASON_VALUES)
def test_stream_turn_frozen_terminal_reasons_are_persisted(store, reason: str):
    session = store.create_session(user_id="user_1", character_id="aemeath")
    reserved = store.resolve_or_reserve(
        request=_resolve_request(
            session_id=session.id,
            client_message_id=f"cm-reason-{reason}",
        )
    )

    failed = store.fail(request=_fail_request(reserved.record, reason=reason))

    assert failed.status == "failed"
    assert failed.terminal_reason == reason


@pytest.mark.parametrize("state_delta_json", ["{}", '{"trust":2,"affection":3}'])
def test_stream_turn_completed_replay_rejects_incomplete_state_delta(
    store,
    state_delta_json: str,
):
    session = store.create_session(user_id="user_1", character_id="aemeath")
    reserved = store.resolve_or_reserve(
        request=_resolve_request(
            session_id=session.id,
            client_message_id="cm-corrupt-completed-delta",
        )
    )
    store.complete(request=_complete_request(reserved.record))
    with sqlite3.connect(store.db_path) as connection:
        connection.execute(
            "UPDATE chat_request_idempotency SET state_delta_json = ? WHERE id = ?",
            (state_delta_json, reserved.record.id),
        )

    with pytest.raises(StorageMigrationRequiredError) as raised:
        store.resolve_or_reserve(
            request=_resolve_request(
                session_id=session.id,
                client_message_id="cm-corrupt-completed-delta",
                turn_id="turn-corrupt-completed-delta-replay",
                user_message_id="message-corrupt-completed-delta-replay",
            )
        )
    assert raised.value.metadata["reason"] == "streaming_chat_record_payload_invalid"


@pytest.mark.parametrize("status", ["in_progress", "failed"])
def test_stream_turn_noncompleted_replay_rejects_nonempty_state_delta(store, status: str):
    session = store.create_session(user_id="user_1", character_id="aemeath")
    reserved = store.resolve_or_reserve(
        request=_resolve_request(
            session_id=session.id,
            client_message_id=f"cm-corrupt-{status}-delta",
        )
    )
    if status == "failed":
        store.fail(request=_fail_request(reserved.record))
    with sqlite3.connect(store.db_path) as connection:
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute(
            "UPDATE chat_request_idempotency SET state_delta_json = ? WHERE id = ?",
            ('{"trust":2,"affection":3,"familiarity":4}', reserved.record.id),
        )
        connection.execute("PRAGMA ignore_check_constraints=OFF")

    with pytest.raises(StorageMigrationRequiredError) as raised:
        store.resolve_or_reserve(
            request=_resolve_request(
                session_id=session.id,
                client_message_id=f"cm-corrupt-{status}-delta",
                turn_id=f"turn-corrupt-{status}-delta-replay",
                user_message_id=f"message-corrupt-{status}-delta-replay",
            )
        )
    assert raised.value.metadata["reason"] == "streaming_chat_record_payload_invalid"


def test_stream_turn_same_key_different_fingerprint_is_conflict(store):
    session = store.create_session(user_id="user_1", character_id="aemeath")
    reserved = store.resolve_or_reserve(
        request=_resolve_request(session_id=session.id, client_message_id="cm-conflict")
    )
    conflict = store.resolve_or_reserve(
        request=_resolve_request(
            session_id=session.id,
            client_message_id="cm-conflict",
            fingerprint="opaque-fingerprint-v2",
            turn_id="turn-conflict-second",
            user_message_id="message-conflict-second",
            message="changed message",
        )
    )

    assert reserved.kind == "reserved"
    assert conflict.kind == "conflict"
    assert conflict.record.id == reserved.record.id
    assert [message.role for message in store.list_messages_by_session(session_id=session.id)] == [
        "user"
    ]


def test_stream_turn_replay_rechecks_owner_and_session_lifecycle(store):
    session = store.create_session(user_id="user_1", character_id="aemeath")
    reserved = store.resolve_or_reserve(
        request=_resolve_request(session_id=session.id, client_message_id="cm-lifecycle")
    )
    store.complete(request=_complete_request(reserved.record))
    store.end_session(session_id=session.id, user_id="user_1")

    ended_replay = store.resolve_or_reserve(
        request=_resolve_request(
            session_id=session.id,
            client_message_id="cm-lifecycle",
            turn_id="turn-ended-replay",
            user_message_id="message-ended-replay",
        )
    )
    assert ended_replay.kind == "replay_completed"
    assert ended_replay.assistant_message is not None

    with pytest.raises(SessionEndedError):
        store.resolve_or_reserve(
            request=_resolve_request(
                session_id=session.id,
                client_message_id="cm-ended-absent",
            )
        )
    with pytest.raises(SessionOwnershipError):
        store.resolve_or_reserve(
            request=_resolve_request(
                session_id=session.id,
                client_message_id="cm-lifecycle",
                user_id="user_2",
            )
        )

    store.archive_session(session_id=session.id, user_id="user_1")
    with pytest.raises(SessionArchivedError):
        store.resolve_or_reserve(
            request=_resolve_request(
                session_id=session.id,
                client_message_id="cm-lifecycle",
            )
        )


def test_stream_turn_completion_rechecks_ended_session_without_assistant(store):
    session = store.create_session(user_id="user_1", character_id="aemeath")
    reserved = store.resolve_or_reserve(
        request=_resolve_request(session_id=session.id, client_message_id="cm-ended-complete")
    )
    store.end_session(session_id=session.id, user_id="user_1")

    result = store.complete(request=_complete_request(reserved.record))

    assert result.status == "failed"
    assert result.terminal_error_code == "SESSION_ENDED"
    assert result.assistant_message_id is None
    assert [message.role for message in store.list_messages_by_session(session_id=session.id)] == [
        "user"
    ]


@pytest.mark.parametrize("backend", ["sqlite_raw", "sqlalchemy"])
def test_stream_turn_same_key_concurrent_reserve_is_exactly_once(tmp_path: Path, backend: str):
    db_path = tmp_path / f"{backend}-reserve.db"
    primary = _make_store(backend, db_path)
    session = primary.create_session(user_id="user_1", character_id="aemeath")
    first_store = _make_store(backend, db_path)
    second_store = _make_store(backend, db_path)
    barrier = Barrier(2)
    requests = [
        _resolve_request(
            session_id=session.id,
            client_message_id="cm-concurrent",
            turn_id="turn-concurrent-1",
            user_message_id="message-concurrent-1",
        ),
        _resolve_request(
            session_id=session.id,
            client_message_id="cm-concurrent",
            turn_id="turn-concurrent-2",
            user_message_id="message-concurrent-2",
        ),
    ]

    def run(store_instance, request):
        barrier.wait(timeout=5)
        return store_instance.resolve_or_reserve(request=request)

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(
            executor.map(
                lambda item: run(*item),
                ((first_store, requests[0]), (second_store, requests[1])),
            )
        )

    assert {outcome.kind for outcome in outcomes} == {"reserved", "in_progress"}
    assert len({outcome.record.id for outcome in outcomes}) == 1
    assert [
        message.role
        for message in primary.list_messages_by_session(session_id=session.id)
    ] == ["user"]


@pytest.mark.parametrize("backend", ["sqlite_raw", "sqlalchemy"])
def test_stream_turn_completion_cancel_cas_and_latest_relationship_delta(
    tmp_path: Path,
    backend: str,
):
    db_path = tmp_path / f"{backend}-complete-race.db"
    primary = _make_store(backend, db_path)
    first_session = primary.create_session(user_id="user_1", character_id="aemeath")
    second_session = primary.create_session(user_id="user_1", character_id="aemeath")
    first = primary.resolve_or_reserve(
        request=_resolve_request(session_id=first_session.id, client_message_id="cm-race")
    )
    first_store = _make_store(backend, db_path)
    second_store = _make_store(backend, db_path)
    barrier = Barrier(2)

    def complete():
        barrier.wait(timeout=5)
        return first_store.complete(request=_complete_request(first.record))

    def cancel():
        barrier.wait(timeout=5)
        return second_store.fail(
            request=_fail_request(first.record, reason="user_cancelled")
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        complete_result = executor.submit(complete)
        cancel_result = executor.submit(cancel)
        results = [complete_result.result(), cancel_result.result()]

    assert len({result.status for result in results}) == 1
    terminal = results[0]
    messages = primary.list_messages_by_session(session_id=first_session.id)
    assert [message.role for message in messages] == (
        ["user", "assistant"] if terminal.status == "completed" else ["user"]
    )

    first_completed = primary.resolve_or_reserve(
        request=_resolve_request(
            session_id=first_session.id,
            client_message_id="cm-delta-1",
            turn_id="turn-delta-1",
            user_message_id="message-delta-1",
        )
    )
    second_completed = primary.resolve_or_reserve(
        request=_resolve_request(
            session_id=second_session.id,
            client_message_id="cm-delta-2",
            turn_id="turn-delta-2",
            user_message_id="message-delta-2",
        )
    )
    delta_barrier = Barrier(2)

    def complete_delta(store_instance, record, update):
        delta_barrier.wait(timeout=5)
        return store_instance.complete(
            request=_complete_request(record, response="delta", update=update)
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        delta_results = list(
            executor.map(
                lambda item: complete_delta(*item),
                (
                    (
                        first_store,
                        first_completed.record,
                        _state_update(trust=3, affection=4, familiarity=5),
                    ),
                    (
                        second_store,
                        second_completed.record,
                        _state_update(trust=7, affection=8, familiarity=9),
                    ),
                ),
            )
        )
    assert [result.status for result in delta_results] == ["completed", "completed"]
    relationship = primary.get_or_create_relationship_state(
        user_id="user_1",
        character_id="aemeath",
    )
    baseline = 50 + (2 if terminal.status == "completed" else 0)
    assert relationship.trust == baseline + 3 + 7
    assert relationship.affection == 50 + (3 if terminal.status == "completed" else 0) + 4 + 8
    assert relationship.familiarity == 50 + (4 if terminal.status == "completed" else 0) + 5 + 9


def test_stream_turn_orphan_recovery_is_bounded_and_terminal(store):
    session = store.create_session(user_id="user_1", character_id="aemeath")
    reserved = store.resolve_or_reserve(
        request=_resolve_request(
            session_id=session.id,
            client_message_id="cm-orphan",
            owner_runtime_id="runtime_old",
        )
    )

    assert store.fail_orphan_if_owned_by_other_runtime(
        owner_runtime_id="runtime_new",
        limit=1,
    ) == 1
    assert store.fail_orphan_if_owned_by_other_runtime(
        owner_runtime_id="runtime_new",
        limit=1,
    ) == 0
    replay = store.resolve_or_reserve(
        request=_resolve_request(
            session_id=session.id,
            client_message_id="cm-orphan",
            owner_runtime_id="runtime_new",
            turn_id="turn-orphan-replay",
            user_message_id="message-orphan-replay",
        )
    )
    assert reserved.kind == "reserved"
    assert replay.kind == "replay_failed"
    assert replay.record.terminal_error_code == "CHAT_STREAM_INTERRUPTED"
    assert replay.record.terminal_reason == "runtime_orphaned"
