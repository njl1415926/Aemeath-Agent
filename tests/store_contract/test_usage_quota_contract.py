from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from app.db.errors import StorageMigrationRequiredError
from app.db.sqlalchemy_store import SqlAlchemyStore
from app.db.sqlite_store import SQLiteStore
from app.db.stream_turns import StreamTurnResolveOrReserve
from app.quota.schemas import (
    ESTIMATOR_VERSION_PROMPT_UTF8_V0_1,
    StreamQuotaComplete,
    StreamQuotaFail,
    StreamQuotaProviderStarted,
    StreamQuotaResolveOrReserve,
    UsageOutputEstimateRequest,
    UsagePromptEstimateRequest,
    UsageQueryRequest,
    UsageQuotaPlanRequest,
)
from app.quota.service import PromptUtf8UsageQuotaPolicy
from app.schemas import StateUpdate


def _make_store(backend: str, db_path: Path):
    if backend == "sqlite_raw":
        return SQLiteStore(db_path)
    if backend == "sqlalchemy":
        return SqlAlchemyStore(db_path)
    raise AssertionError(f"unknown backend {backend!r}")


def _policy() -> PromptUtf8UsageQuotaPolicy:
    return PromptUtf8UsageQuotaPolicy()


def _plan(
    *,
    user_id: str = "user_1",
    character_id: str = "aemeath",
    quota_day: str = "2026-08-10",
    turn_limit: int = 50,
    token_limit: int = 1000,
    context_budget_max_chars: int = 60,
    max_output_tokens: int = 20,
):
    return _policy().plan_stream_reservation(
        request=UsageQuotaPlanRequest(
            user_id=user_id,
            quota_day=quota_day,
            surface="stream_chat",
            character_id=character_id,
            turn_limit=turn_limit,
            token_limit=token_limit,
            estimator_version=ESTIMATOR_VERSION_PROMPT_UTF8_V0_1,
            context_budget_max_chars=context_budget_max_chars,
            max_output_tokens=max_output_tokens,
        )
    )


def _stream_request(
    *,
    session_id: str,
    client_message_id: str,
    user_id: str = "user_1",
    owner_runtime_id: str = "runtime_1",
    fingerprint: str = "fingerprint_v1",
    turn_id: str | None = None,
    user_message_id: str | None = None,
):
    suffix = client_message_id.replace("-", "_")
    return StreamTurnResolveOrReserve(
        user_id=user_id,
        session_id=session_id,
        active_character_id="aemeath",
        client_message_id=client_message_id,
        request_fingerprint_version=1,
        request_fingerprint=fingerprint,
        owner_runtime_id=owner_runtime_id,
        turn_id=turn_id or f"turn_{suffix}",
        user_message_id=user_message_id or f"message_{suffix}",
        user_message_content="hello",
    )


def _reserve(store, *, session_id: str, client_message_id: str, **kwargs):
    stream = _stream_request(session_id=session_id, client_message_id=client_message_id, **kwargs)
    return store.resolve_or_reserve_with_quota(
        request=StreamQuotaResolveOrReserve(
            stream_request=stream,
            quota_plan=_plan(user_id=stream.user_id),
            retry_after_seconds=60,
        )
    )


def _prompt(bytes_count: int = 30):
    return _policy().estimate_prompt(
        request=UsagePromptEstimateRequest(
            estimator_version=ESTIMATOR_VERSION_PROMPT_UTF8_V0_1,
            prompt_messages_utf8_bytes=bytes_count,
            max_output_tokens=20,
        )
    )


def _output(bytes_count: int = 12, delta_count: int = 2):
    return _policy().estimate_output(
        request=UsageOutputEstimateRequest(
            estimator_version=ESTIMATOR_VERSION_PROMPT_UTF8_V0_1,
            response_utf8_bytes=bytes_count,
            delta_count=delta_count,
            max_output_tokens=20,
        )
    )


def _state_update() -> StateUpdate:
    return StateUpdate(
        user_emotion="glad",
        aemeath_mood="warm",
        trust_delta=1,
        affection_delta=2,
        familiarity_delta=3,
        last_topic="quota",
        reason="test",
    )


def _complete(store, record, *, response: str = "durable assistant"):
    return store.complete_with_quota(
        request=StreamQuotaComplete(
            stream_request_id=record.id,
            turn_id=record.turn_id,
            user_id=record.user_id,
            session_id=record.session_id,
            owner_runtime_id=record.owner_runtime_id,
            assistant_response=response,
            state_update=_state_update(),
            output_estimate=_output(),
            warnings=("model_output_truncated",),
        )
    )


@pytest.mark.parametrize("backend", ["sqlite_raw", "sqlalchemy"])
def test_stream_quota_reserve_provider_started_complete_and_replay(
    tmp_path: Path,
    backend: str,
):
    store = _make_store(backend, tmp_path / f"{backend}.db")
    session = store.create_session(user_id="user_1", character_id="aemeath")

    reserved = _reserve(store, session_id=session.id, client_message_id="cm-complete")
    assert reserved.kind == "reserved"
    assert reserved.quota_reservation.status == "reserved"
    assert reserved.daily_summary.reserved_turns == 1
    assert [m.role for m in store.list_messages_by_session(session_id=session.id)] == ["user"]

    duplicate = _reserve(store, session_id=session.id, client_message_id="cm-complete")
    assert duplicate.kind == "in_progress"
    assert store.get_daily_usage(
        request=UsageQueryRequest(user_id="user_1", quota_day="2026-08-10")
    ).reserved_turns == 1

    provider_started = store.mark_provider_started_with_quota(
        request=StreamQuotaProviderStarted(
            stream_request_id=reserved.record.id,
            turn_id=reserved.record.turn_id,
            user_id="user_1",
            session_id=session.id,
            owner_runtime_id="runtime_1",
            prompt_estimate=_prompt(30),
            retry_after_seconds=60,
        )
    )
    assert provider_started.charging_phase == "provider_started"
    assert provider_started.prompt_messages_utf8_bytes == 30

    completed = _complete(store, reserved.record)
    assert completed.stream_record.status == "completed"
    assert completed.quota_reservation.status == "settled"
    assert completed.daily_summary.reserved_turns == 0
    assert completed.daily_summary.settled_turns == 1
    assert completed.daily_summary.settled_tokens == 14

    replay = store.resolve_or_reserve_with_quota(
        request=StreamQuotaResolveOrReserve(
            stream_request=_stream_request(
                session_id=session.id,
                client_message_id="cm-complete",
                turn_id="turn_replay",
                user_message_id="message_replay",
            ),
            quota_plan=_plan(),
            retry_after_seconds=60,
        )
    )
    assert replay.kind == "replay_completed"
    assert replay.assistant_message.content == "durable assistant"
    assert [m.role for m in store.list_messages_by_session(session_id=session.id)] == [
        "user",
        "assistant",
    ]


@pytest.mark.parametrize("backend", ["sqlite_raw", "sqlalchemy"])
def test_stream_quota_rejection_writes_no_stream_message_or_reservation(
    tmp_path: Path,
    backend: str,
):
    store = _make_store(backend, tmp_path / f"{backend}.db")
    session = store.create_session(user_id="user_1", character_id="aemeath")
    stream = _stream_request(session_id=session.id, client_message_id="cm-reject")
    outcome = store.resolve_or_reserve_with_quota(
        request=StreamQuotaResolveOrReserve(
            stream_request=stream,
            quota_plan=_plan(token_limit=1),
            retry_after_seconds=60,
        )
    )

    assert outcome.kind == "quota_exceeded"
    assert outcome.retry_after_seconds == 60
    assert store.list_messages_by_session(session_id=session.id) == []
    with sqlite3.connect(store.db_path) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM chat_request_idempotency").fetchone()[0]
            == 0
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM usage_quota_reservations").fetchone()[0]
            == 0
        )


@pytest.mark.parametrize("backend", ["sqlite_raw", "sqlalchemy"])
def test_stream_quota_conflict_does_not_mutate_counters(tmp_path: Path, backend: str):
    store = _make_store(backend, tmp_path / f"{backend}.db")
    session = store.create_session(user_id="user_1", character_id="aemeath")
    first = _reserve(store, session_id=session.id, client_message_id="cm-conflict")
    before = store.get_daily_usage(
        request=UsageQueryRequest(user_id="user_1", quota_day="2026-08-10")
    )

    conflict = store.resolve_or_reserve_with_quota(
        request=StreamQuotaResolveOrReserve(
            stream_request=_stream_request(
                session_id=session.id,
                client_message_id="cm-conflict",
                fingerprint="different",
                turn_id="turn_conflict_2",
                user_message_id="message_conflict_2",
            ),
            quota_plan=_plan(),
            retry_after_seconds=60,
        )
    )

    assert first.kind == "reserved"
    assert conflict.kind == "conflict"
    after = store.get_daily_usage(
        request=UsageQueryRequest(user_id="user_1", quota_day="2026-08-10")
    )
    assert after.reserved_turns == before.reserved_turns
    assert after.reserved_tokens == before.reserved_tokens
    assert [m.role for m in store.list_messages_by_session(session_id=session.id)] == ["user"]


@pytest.mark.parametrize("backend", ["sqlite_raw", "sqlalchemy"])
def test_stream_quota_same_client_message_id_is_scoped_by_session(
    tmp_path: Path,
    backend: str,
):
    store = _make_store(backend, tmp_path / f"{backend}.db")
    first_session = store.create_session(user_id="user_1", character_id="aemeath")
    second_session = store.create_session(user_id="user_1", character_id="aemeath")

    first = _reserve(
        store,
        session_id=first_session.id,
        client_message_id="cm-shared",
        turn_id="turn_shared_1",
        user_message_id="message_shared_1",
    )
    second = _reserve(
        store,
        session_id=second_session.id,
        client_message_id="cm-shared",
        turn_id="turn_shared_2",
        user_message_id="message_shared_2",
    )

    assert first.kind == "reserved"
    assert second.kind == "reserved"
    daily = store.get_daily_usage(
        request=UsageQueryRequest(user_id="user_1", quota_day="2026-08-10")
    )
    assert daily.reserved_turns == 2
    with sqlite3.connect(store.db_path) as connection:
        keys = [
            row[0]
            for row in connection.execute(
                """
                SELECT idempotency_key
                FROM usage_quota_reservations
                ORDER BY session_id
                """
            )
        ]
    assert len(keys) == 2
    assert keys[0] != keys[1]
    assert all("cm-shared" in key for key in keys)


@pytest.mark.parametrize("backend", ["sqlite_raw", "sqlalchemy"])
def test_stream_quota_complete_requires_provider_started(tmp_path: Path, backend: str):
    store = _make_store(backend, tmp_path / f"{backend}.db")
    session = store.create_session(user_id="user_1", character_id="aemeath")
    reserved = _reserve(store, session_id=session.id, client_message_id="cm-no-provider")

    with pytest.raises(StorageMigrationRequiredError) as raised:
        _complete(store, reserved.record)

    assert raised.value.metadata["reason"] == "usage_quota_provider_not_started"
    assert [m.role for m in store.list_messages_by_session(session_id=session.id)] == ["user"]
    daily = store.get_daily_usage(
        request=UsageQueryRequest(user_id="user_1", quota_day="2026-08-10")
    )
    assert daily.reserved_turns == 1
    assert daily.settled_turns == 0
    with sqlite3.connect(store.db_path) as connection:
        stream = connection.execute(
            "SELECT status FROM chat_request_idempotency WHERE id = ?",
            (reserved.record.id,),
        ).fetchone()
        quota = connection.execute(
            """
            SELECT status, charging_phase
            FROM usage_quota_reservations
            WHERE stream_request_id = ?
            """,
            (reserved.record.id,),
        ).fetchone()
    assert stream[0] == "in_progress"
    assert quota == ("reserved", "reserved_pre_provider")


@pytest.mark.parametrize("backend", ["sqlite_raw", "sqlalchemy"])
def test_stream_quota_live_owner_mismatch_fails_closed(tmp_path: Path, backend: str):
    store = _make_store(backend, tmp_path / f"{backend}.db")
    session = store.create_session(user_id="user_1", character_id="aemeath")
    reserved = _reserve(store, session_id=session.id, client_message_id="cm-owner")

    with pytest.raises(StorageMigrationRequiredError) as provider_error:
        store.mark_provider_started_with_quota(
            request=StreamQuotaProviderStarted(
                stream_request_id=reserved.record.id,
                turn_id=reserved.record.turn_id,
                user_id="user_1",
                session_id=session.id,
                owner_runtime_id="wrong_runtime",
                prompt_estimate=_prompt(30),
                retry_after_seconds=60,
            )
        )
    with pytest.raises(StorageMigrationRequiredError) as complete_error:
        store.complete_with_quota(
            request=StreamQuotaComplete(
                stream_request_id=reserved.record.id,
                turn_id=reserved.record.turn_id,
                user_id="user_1",
                session_id=session.id,
                owner_runtime_id="wrong_runtime",
                assistant_response="should not persist",
                state_update=_state_update(),
                output_estimate=_output(),
            )
        )
    with pytest.raises(StorageMigrationRequiredError) as fail_error:
        store.fail_with_quota(
            request=StreamQuotaFail(
                stream_request_id=reserved.record.id,
                turn_id=reserved.record.turn_id,
                user_id="user_1",
                session_id=session.id,
                owner_runtime_id="wrong_runtime",
                terminal_error_code="MODEL_TIMEOUT",
                terminal_retryable=True,
                terminal_reason="model_failed",
            )
        )

    assert provider_error.value.metadata["reason"] == "usage_quota_owner_mismatch"
    assert complete_error.value.metadata["reason"] == "usage_quota_owner_mismatch"
    assert fail_error.value.metadata["reason"] == "usage_quota_owner_mismatch"
    assert [m.role for m in store.list_messages_by_session(session_id=session.id)] == ["user"]
    with sqlite3.connect(store.db_path) as connection:
        stream = connection.execute(
            "SELECT status, owner_runtime_id FROM chat_request_idempotency WHERE id = ?",
            (reserved.record.id,),
        ).fetchone()
        quota = connection.execute(
            """
            SELECT status, charging_phase, owner_runtime_id
            FROM usage_quota_reservations
            WHERE stream_request_id = ?
            """,
            (reserved.record.id,),
        ).fetchone()
    assert stream == ("in_progress", "runtime_1")
    assert quota == ("reserved", "reserved_pre_provider", "runtime_1")


@pytest.mark.parametrize("backend", ["sqlite_raw", "sqlalchemy"])
def test_stream_quota_provider_started_adjustment_can_release_before_provider(
    tmp_path: Path,
    backend: str,
):
    store = _make_store(backend, tmp_path / f"{backend}.db")
    session = store.create_session(user_id="user_1", character_id="aemeath")
    stream = _stream_request(session_id=session.id, client_message_id="cm-provider-reject")
    plan = _plan(token_limit=120, context_budget_max_chars=60, max_output_tokens=20)
    reserved = store.resolve_or_reserve_with_quota(
        request=StreamQuotaResolveOrReserve(
            stream_request=stream,
            quota_plan=plan,
            retry_after_seconds=60,
        )
    )
    assert reserved.kind == "reserved"

    released = store.mark_provider_started_with_quota(
        request=StreamQuotaProviderStarted(
            stream_request_id=reserved.record.id,
            turn_id=reserved.record.turn_id,
            user_id="user_1",
            session_id=session.id,
            owner_runtime_id="runtime_1",
            prompt_estimate=_prompt(400),
            retry_after_seconds=60,
        )
    )

    assert released.status == "released"
    assert released.terminal_reason == "quota_rejected_after_prepare"
    daily = store.get_daily_usage(
        request=UsageQueryRequest(user_id="user_1", quota_day="2026-08-10")
    )
    assert daily.reserved_turns == 0
    assert daily.released_turns == 1


@pytest.mark.parametrize("backend", ["sqlite_raw", "sqlalchemy"])
def test_stream_quota_fail_releases_before_provider_and_settles_after_provider(
    tmp_path: Path,
    backend: str,
):
    store = _make_store(backend, tmp_path / f"{backend}.db")
    session = store.create_session(user_id="user_1", character_id="aemeath")
    pre = _reserve(store, session_id=session.id, client_message_id="cm-pre")
    pre_failed = store.fail_with_quota(
        request=StreamQuotaFail(
            stream_request_id=pre.record.id,
            turn_id=pre.record.turn_id,
            user_id="user_1",
            session_id=session.id,
            owner_runtime_id="runtime_1",
            terminal_error_code="MODEL_TIMEOUT",
            terminal_retryable=True,
            terminal_reason="preparation_failed",
        )
    )
    assert pre_failed.quota_reservation.status == "released"

    post = _reserve(store, session_id=session.id, client_message_id="cm-post")
    store.mark_provider_started_with_quota(
        request=StreamQuotaProviderStarted(
            stream_request_id=post.record.id,
            turn_id=post.record.turn_id,
            user_id="user_1",
            session_id=session.id,
            owner_runtime_id="runtime_1",
            prompt_estimate=_prompt(30),
            retry_after_seconds=60,
        )
    )
    post_failed = store.fail_with_quota(
        request=StreamQuotaFail(
            stream_request_id=post.record.id,
            turn_id=post.record.turn_id,
            user_id="user_1",
            session_id=session.id,
            owner_runtime_id="runtime_1",
            terminal_error_code="MODEL_TIMEOUT",
            terminal_retryable=True,
            terminal_reason="model_failed",
        )
    )

    assert post_failed.quota_reservation.status == "settled"
    daily = store.get_daily_usage(
        request=UsageQueryRequest(user_id="user_1", quota_day="2026-08-10")
    )
    assert daily.released_turns == 1
    assert daily.settled_turns == 1


@pytest.mark.parametrize("backend", ["sqlite_raw", "sqlalchemy"])
def test_stream_quota_recovery_uses_owner_and_charging_phase(tmp_path: Path, backend: str):
    store = _make_store(backend, tmp_path / f"{backend}.db")
    session = store.create_session(user_id="user_1", character_id="aemeath")
    _reserve(
        store,
        session_id=session.id,
        client_message_id="cm-orphan-pre",
        owner_runtime_id="old_runtime",
    )
    post = _reserve(
        store,
        session_id=session.id,
        client_message_id="cm-orphan-post",
        owner_runtime_id="old_runtime",
    )
    store.mark_provider_started_with_quota(
        request=StreamQuotaProviderStarted(
            stream_request_id=post.record.id,
            turn_id=post.record.turn_id,
            user_id="user_1",
            session_id=session.id,
            owner_runtime_id="old_runtime",
            prompt_estimate=_prompt(30),
            retry_after_seconds=60,
        )
    )

    recovered = store.recover_orphaned_with_quota(owner_runtime_id="new_runtime")

    assert recovered.recovered_count == 2
    assert recovered.released_count == 1
    assert recovered.settled_count == 1
    with sqlite3.connect(store.db_path) as connection:
        rows = {
            row[0]: row[1]
            for row in connection.execute(
                """
                SELECT c.client_message_id, q.terminal_reason
                FROM usage_quota_reservations AS q
                INNER JOIN chat_request_idempotency AS c ON c.id = q.stream_request_id
                ORDER BY c.client_message_id
                """
            )
        }
    assert rows["cm-orphan-pre"] == "runtime_orphaned_pre_provider"
    assert rows["cm-orphan-post"] == "runtime_orphaned_after_provider"


@pytest.mark.parametrize("backend", ["sqlite_raw", "sqlalchemy"])
def test_stream_quota_cross_midnight_settles_original_quota_day(
    tmp_path: Path,
    backend: str,
):
    store = _make_store(backend, tmp_path / f"{backend}.db")
    session = store.create_session(user_id="user_1", character_id="aemeath")
    reserved = store.resolve_or_reserve_with_quota(
        request=StreamQuotaResolveOrReserve(
            stream_request=_stream_request(session_id=session.id, client_message_id="cm-midnight"),
            quota_plan=_plan(quota_day="2026-08-10"),
            retry_after_seconds=60,
        )
    )
    store.mark_provider_started_with_quota(
        request=StreamQuotaProviderStarted(
            stream_request_id=reserved.record.id,
            turn_id=reserved.record.turn_id,
            user_id="user_1",
            session_id=session.id,
            owner_runtime_id="runtime_1",
            prompt_estimate=_prompt(30),
            retry_after_seconds=60,
        )
    )

    _complete(store, reserved.record)

    assert store.get_daily_usage(
        request=UsageQueryRequest(user_id="user_1", quota_day="2026-08-10")
    ).settled_turns == 1
    assert (
        store.get_daily_usage(
            request=UsageQueryRequest(user_id="user_1", quota_day="2026-08-11")
        )
        is None
    )


@pytest.mark.parametrize("backend", ["sqlite_raw", "sqlalchemy"])
def test_stream_quota_daily_policy_is_frozen(tmp_path: Path, backend: str):
    store = _make_store(backend, tmp_path / f"{backend}.db")
    session = store.create_session(user_id="user_1", character_id="aemeath")
    _reserve(store, session_id=session.id, client_message_id="cm-freeze")

    with pytest.raises(StorageMigrationRequiredError) as raised:
        store.resolve_or_reserve_with_quota(
            request=StreamQuotaResolveOrReserve(
                stream_request=_stream_request(
                    session_id=session.id,
                    client_message_id="cm-freeze-2",
                ),
                quota_plan=_plan(token_limit=2000),
                retry_after_seconds=60,
            )
        )
    assert raised.value.metadata["reason"] == "usage_quota_daily_policy_mismatch"


@pytest.mark.parametrize("backend", ["sqlite_raw", "sqlalchemy"])
def test_stream_quota_concurrent_reserve_honors_turn_limit(tmp_path: Path, backend: str):
    db_path = tmp_path / f"{backend}.db"
    store = _make_store(backend, db_path)
    session = store.create_session(user_id="user_1", character_id="aemeath")
    barrier = Barrier(2)

    def reserve(index: int):
        worker_store = _make_store(backend, db_path)
        barrier.wait(timeout=5)
        return worker_store.resolve_or_reserve_with_quota(
            request=StreamQuotaResolveOrReserve(
                stream_request=_stream_request(
                    session_id=session.id,
                    client_message_id=f"cm-concurrent-{index}",
                    turn_id=f"turn_concurrent_{index}",
                    user_message_id=f"message_concurrent_{index}",
                ),
                quota_plan=_plan(turn_limit=1),
                retry_after_seconds=60,
            )
        ).kind

    with ThreadPoolExecutor(max_workers=2) as pool:
        kinds = sorted(pool.map(reserve, [1, 2]))

    assert kinds == ["quota_exceeded", "reserved"]
    daily = store.get_daily_usage(
        request=UsageQueryRequest(user_id="user_1", quota_day="2026-08-10")
    )
    assert daily.reserved_turns == 1
    assert len(store.list_messages_by_session(session_id=session.id)) == 1
