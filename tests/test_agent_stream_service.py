from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Any, Iterator

import pytest

import app.services.agent_stream_service as agent_stream_service_module
from app.db.stream_turns import StreamTurnResolveOrReserve
from app.errors import AemeathError, ErrorCode
from app.main import build_application_runtime
from app.model.mock_streaming import MockStreamingChatModelClient
from app.model.streaming import ModelStreamFinished, ModelStreamRequest, ModelTextDelta
from app.quota.schemas import UsageQueryRequest
from app.runtime.guardrails.errors import RuntimeBusyError
from app.services.agent_stream_service import (
    RefCountedStreamKeyRegistry,
    StreamServiceLimits,
)
from app.services.schemas import UserContext
from app.services.stream_fingerprint import fingerprint_chat_request_v1
from app.services.stream_schemas import (
    ResponseDelta,
    StreamChatRequest,
    StreamConsumerSlowError,
    TurnCompleted,
    TurnFailed,
)
from app.session.errors import (
    SessionArchivedError,
    SessionEndedError,
    SessionOwnershipError,
)
from app.utils.ids import new_id


class _RecordingSink:
    def __init__(self, *, slow_once: bool = False) -> None:
        self._lock = Lock()
        self.events: list[Any] = []
        self.closed = Event()
        self.put_after_close = False
        self._slow_once = slow_once

    def put_from_producer(self, *, event: Any, timeout_seconds: float) -> None:
        assert timeout_seconds > 0
        if self.closed.is_set():
            self.put_after_close = True
        if self._slow_once and isinstance(event, ResponseDelta):
            self._slow_once = False
            raise StreamConsumerSlowError("bounded test sink")
        with self._lock:
            self.events.append(event)

    def close_from_producer(self) -> None:
        self.closed.set()


class _RejectingSink:
    def __init__(self, *, error: BaseException) -> None:
        self.error = error
        self.events: list[Any] = []
        self.closed = Event()

    def put_from_producer(self, *, event: Any, timeout_seconds: float) -> None:
        del event
        assert timeout_seconds > 0
        raise self.error

    def close_from_producer(self) -> None:
        self.closed.set()


class _BlockingSession:
    def __init__(self) -> None:
        self.entered = Event()
        self.release = Event()
        self.close_count = 0
        self._cancelled = False

    def __iter__(self):
        yield ModelTextDelta("partial")
        self.entered.set()
        self.release.wait(timeout=2)
        if self._cancelled:
            return
        yield ModelStreamFinished("complete")

    def cancel(self, *, reason: str) -> None:
        del reason
        self._cancelled = True
        self.release.set()

    def close(self) -> None:
        if self.close_count == 0:
            self.close_count = 1
        self.release.set()


class _BlockingStreamingClient:
    def __init__(self) -> None:
        self.session = _BlockingSession()
        self.open_count = 0

    @contextmanager
    def open_stream(self, *, request: ModelStreamRequest) -> Iterator[_BlockingSession]:
        del request
        self.open_count += 1
        try:
            yield self.session
        finally:
            self.session.close()


class _LiveHandle:
    def __init__(self, turn_id: str) -> None:
        self.handle_id = new_id("live")
        self.turn_id = turn_id
        self.owns_in_progress_turn = True

    def cancel(self, *, reason: str) -> None:
        del reason

    def close(self) -> None:
        return None


class _CaptureLogger:
    def __init__(self) -> None:
        self.records: list[Any] = []

    def record(self, record: Any) -> None:
        self.records.append(record)


@pytest.fixture()
def stream_runtime(settings):
    runtime = build_application_runtime(
        settings=replace(
            settings,
            runtime_session_wait_timeout_seconds=0.05,
            deepseek_read_timeout_seconds=0.2,
        ),
        surface="api",
    )
    try:
        yield runtime
    finally:
        runtime.close()


def _request(*, key: str = "cm-test", message: str = "hello", metadata=None):
    return StreamChatRequest(
        session_id="",
        character_id="aemeath",
        message=message,
        client_message_id=key,
        metadata=metadata or {},
    )


def _session_request(runtime, *, key="cm-test", message="hello", metadata=None):
    session = runtime.store.create_session(
        user_id=runtime.settings.default_user_id,
        character_id=runtime.settings.default_character_id,
    )
    return session, replace(
        _request(key=key, message=message, metadata=metadata),
        session_id=session.id,
    )


def _run_handle(handle) -> list[Any]:
    sink = _RecordingSink()
    handle.start(sink=sink)
    assert sink.closed.wait(timeout=3)
    handle.close()
    return [handle.started_event, *sink.events]


def _quota_runtime(settings, **overrides):
    values = {
        "runtime_session_wait_timeout_seconds": 0.05,
        "deepseek_read_timeout_seconds": 0.2,
        "usage_quota_enabled": True,
        "usage_quota_daily_turn_limit": 5,
        "usage_quota_daily_token_limit": 50_000,
        "usage_quota_retry_after_seconds": 7,
    }
    values.update(overrides)
    configured = replace(settings, **values)
    return build_application_runtime(settings=configured, surface="api")


def _quota_reservation_row(runtime, *, turn_id: str):
    with sqlite3.connect(runtime.settings.db_path) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute(
            "SELECT * FROM usage_quota_reservations WHERE turn_id = ?",
            (turn_id,),
        ).fetchone()


def _stream_row(runtime, *, turn_id: str):
    with sqlite3.connect(runtime.settings.db_path) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute(
            "SELECT * FROM chat_request_idempotency WHERE turn_id = ?",
            (turn_id,),
        ).fetchone()


def _quota_reservation_count(runtime) -> int:
    with sqlite3.connect(runtime.settings.db_path) as connection:
        return connection.execute(
            "SELECT COUNT(*) FROM usage_quota_reservations"
        ).fetchone()[0]


def _context(runtime, request_id: str) -> UserContext:
    return UserContext(user_id=runtime.settings.default_user_id, request_id=request_id)


def _resolve_existing(runtime, service, request):
    return runtime.store.resolve_or_reserve(
        request=StreamTurnResolveOrReserve(
            user_id=runtime.settings.default_user_id,
            session_id=request.session_id,
            active_character_id="aemeath",
            client_message_id=request.client_message_id,
            request_fingerprint_version=1,
            request_fingerprint=fingerprint_chat_request_v1(
                character_id=request.character_id,
                message=request.message,
                metadata=request.metadata,
            ),
            owner_runtime_id=service.owner_runtime_id,
            turn_id=new_id("turn"),
            user_message_id=new_id("message"),
            user_message_content=request.message,
        )
    )


def test_fingerprint_v1_golden_vectors_and_key_order() -> None:
    assert fingerprint_chat_request_v1(
        character_id="\u827e\u6885\u65af",
        message="\u4f60\u597d\n",
        metadata={"z": [1, -0.0, True, None], "\u00e9": {"x": 1.5}},
    ) == "c25749f6076416e2cd25da6584a3ce9f960148f93da484c6586cea295ea26e4b"
    expected = "189cfdf6fa701c18aa9b6233d7c7e397e0f5d1a94fca368853631eeecf7becc9"
    assert fingerprint_chat_request_v1(
        character_id="aemeath",
        message="hello",
        metadata={"b": 2, "a": 1},
    ) == expected
    assert fingerprint_chat_request_v1(
        character_id="aemeath",
        message="hello",
        metadata={"a": 1, "b": 2},
    ) == expected
    assert fingerprint_chat_request_v1(
        character_id="aemeath",
        message="m",
        metadata={"value": -0.0},
    ) == "d61b79969a0a25c7a5c79732164b6ab31a89443387697f3f9e6c448ef8806b75"


def test_quota_enabled_stream_settles_after_provider_started(settings) -> None:
    runtime = _quota_runtime(settings)
    try:
        service = runtime.agent_stream_service
        assert service is not None
        session, request = _session_request(runtime, key="cm-quota-success")

        events = _run_handle(
            service.stream_chat(user_context=_context(runtime, "req_quota"), request=request)
        )

        assert [event.event_type for event in events] == [
            "turn.started",
            "response.delta",
            "turn.completed",
        ]
        row = _quota_reservation_row(runtime, turn_id=events[-1].turn_id)
        assert row is not None
        assert row["status"] == "settled"
        assert row["charging_phase"] == "terminalized"
        assert row["provider_started_at"] is not None
        assert row["prompt_messages_utf8_bytes"] > 0
        assert row["output_utf8_bytes"] > 0
        summary = runtime.store.get_daily_usage(
            request=UsageQueryRequest(
                user_id=runtime.settings.default_user_id,
                quota_day=row["quota_day"],
            )
        )
        assert summary is not None
        assert summary.reserved_turns == 0
        assert summary.settled_turns == 1
    finally:
        runtime.close()


def test_quota_prompt_adjustment_releases_before_provider_open(settings) -> None:
    runtime = _quota_runtime(
        settings,
        context_budget_max_chars=1,
        deepseek_max_output_tokens=1,
        usage_quota_daily_token_limit=10,
    )
    try:
        service = runtime.agent_stream_service
        assert service is not None
        model = service.model_client.delegate
        session, request = _session_request(runtime, key="cm-quota-prompt-reject")

        events = _run_handle(
            service.stream_chat(user_context=_context(runtime, "req_quota_reject"), request=request)
        )

        assert model.open_count == 0
        assert isinstance(events[-1], TurnFailed)
        assert events[-1].error.code == ErrorCode.USAGE_QUOTA_EXCEEDED.value
        row = _quota_reservation_row(runtime, turn_id=events[-1].turn_id)
        assert row is not None
        assert row["status"] == "released"
        assert row["charging_phase"] == "terminalized"
        assert row["provider_started_at"] is None
        assert row["terminal_reason"] == "quota_rejected_after_prepare"
    finally:
        runtime.close()


def test_quota_provider_failure_settles_after_provider_started(settings) -> None:
    runtime = _quota_runtime(settings)
    try:
        service = runtime.agent_stream_service
        assert service is not None
        service.model_client = MockStreamingChatModelClient(
            failure=AemeathError(
                code=ErrorCode.MODEL_UNAVAILABLE,
                public_message="The model service is temporarily unavailable.",
                internal_message="safe test failure",
                retryable=True,
            ),
            fail_after_events=0,
        )
        session, request = _session_request(runtime, key="cm-quota-provider-fail")

        events = _run_handle(
            service.stream_chat(
                user_context=_context(runtime, "req_provider_fail"),
                request=request,
            )
        )

        assert isinstance(events[-1], TurnFailed)
        assert events[-1].error.code == ErrorCode.MODEL_UNAVAILABLE.value
        row = _quota_reservation_row(runtime, turn_id=events[-1].turn_id)
        assert row is not None
        assert row["status"] == "settled"
        assert row["charging_phase"] == "terminalized"
        assert row["provider_started_at"] is not None
        assert row["terminal_reason"] == "model_failed"
    finally:
        runtime.close()


def test_quota_pre_provider_cancel_releases(settings) -> None:
    runtime = _quota_runtime(settings)
    try:
        service = runtime.agent_stream_service
        assert service is not None
        session, request = _session_request(runtime, key="cm-quota-pre-cancel")
        handle = service.stream_chat(
            user_context=_context(runtime, "req_pre_cancel"),
            request=request,
        )
        handle.cancel(reason="user_cancelled")

        events = _run_handle(handle)

        assert isinstance(events[-1], TurnFailed)
        assert events[-1].error.code == ErrorCode.CHAT_STREAM_CANCELLED.value
        row = _quota_reservation_row(runtime, turn_id=events[-1].turn_id)
        assert row is not None
        assert row["status"] == "released"
        assert row["charging_phase"] == "terminalized"
        assert row["provider_started_at"] is None
        assert row["terminal_reason"] == "user_cancelled"
        assert service.model_client.delegate.open_count == 0
    finally:
        runtime.close()


@pytest.mark.parametrize("reason", ["user_cancelled", "client_disconnected"])
def test_quota_post_provider_cancel_or_disconnect_settles(settings, reason) -> None:
    runtime = _quota_runtime(settings)
    try:
        service = runtime.agent_stream_service
        assert service is not None
        blocking = _BlockingStreamingClient()
        service.model_client = blocking
        session, request = _session_request(runtime, key=f"cm-quota-{reason}")
        handle = service.stream_chat(
            user_context=_context(runtime, f"req_{reason}"),
            request=request,
        )
        sink = _RecordingSink()

        handle.start(sink=sink)
        assert blocking.session.entered.wait(timeout=3)
        handle.cancel(reason=reason)
        assert sink.closed.wait(timeout=3)
        handle.close()
        events = [handle.started_event, *sink.events]

        assert isinstance(events[-1], TurnFailed)
        row = _quota_reservation_row(runtime, turn_id=events[-1].turn_id)
        assert row is not None
        assert row["status"] == "settled"
        assert row["charging_phase"] == "terminalized"
        assert row["provider_started_at"] is not None
        assert row["terminal_reason"] == reason
        assert blocking.open_count == 1
    finally:
        runtime.close()


def test_quota_durable_completion_late_cancel_preserves_first_wins(settings) -> None:
    runtime = _quota_runtime(settings)
    try:
        service = runtime.agent_stream_service
        assert service is not None
        session, request = _session_request(runtime, key="cm-quota-late-cancel")
        handle = service.stream_chat(user_context=_context(runtime, "req_late"), request=request)
        events = _run_handle(handle)
        assert isinstance(events[-1], TurnCompleted)

        handle.cancel(reason="user_cancelled")
        handle.close()

        stream = _stream_row(runtime, turn_id=events[-1].turn_id)
        quota = _quota_reservation_row(runtime, turn_id=events[-1].turn_id)
        assert stream["status"] == "completed"
        assert stream["terminal_reason"] is None
        assert quota["status"] == "settled"
        assert quota["terminal_reason"] is None
        replay = service.stream_chat(user_context=_context(runtime, "req_replay"), request=request)
        assert isinstance(_run_handle(replay)[-1], TurnCompleted)
    finally:
        runtime.close()


def test_quota_startup_recovers_orphans_across_multiple_batches(settings) -> None:
    old_runtime = _quota_runtime(
        settings,
        stream_orphan_recovery_limit=1,
        usage_quota_daily_token_limit=100_000,
    )
    new_runtime = None
    try:
        old_service = old_runtime.agent_stream_service
        assert old_service is not None
        requests = []
        for index in range(2):
            session, request = _session_request(old_runtime, key=f"cm-quota-orphan-{index}")
            old_service.stream_chat(
                user_context=_context(old_runtime, f"req_old_{index}"),
                request=request,
            )
            requests.append(request)

        new_runtime = _quota_runtime(
            settings,
            stream_orphan_recovery_limit=1,
            usage_quota_daily_token_limit=100_000,
        )
        new_service = new_runtime.agent_stream_service
        assert new_service is not None

        for request in requests:
            events = _run_handle(
                new_service.stream_chat(
                    user_context=_context(new_runtime, f"req_recover_{request.client_message_id}"),
                    request=request,
                )
            )
            assert isinstance(events[-1], TurnFailed)
            row = _quota_reservation_row(new_runtime, turn_id=events[-1].turn_id)
            assert row["status"] == "released"
            assert row["terminal_reason"] == "runtime_orphaned_pre_provider"
    finally:
        if new_runtime is not None:
            new_runtime.close()
        old_runtime.close()


def test_quota_startup_recovers_legacy_in_progress_without_reservation(settings) -> None:
    old_runtime = build_application_runtime(
        settings=replace(
            settings,
            runtime_session_wait_timeout_seconds=0.05,
            deepseek_read_timeout_seconds=0.2,
            stream_orphan_recovery_limit=1,
            usage_quota_enabled=False,
        ),
        surface="api",
    )
    new_runtime = None
    try:
        old_service = old_runtime.agent_stream_service
        assert old_service is not None
        session, request = _session_request(old_runtime, key="cm-legacy-orphan")
        old_runtime.store.resolve_or_reserve(
            request=StreamTurnResolveOrReserve(
                user_id=old_runtime.settings.default_user_id,
                session_id=session.id,
                active_character_id="aemeath",
                client_message_id=request.client_message_id,
                request_fingerprint_version=1,
                request_fingerprint=fingerprint_chat_request_v1(
                    character_id=request.character_id,
                    message=request.message,
                    metadata=request.metadata,
                ),
                owner_runtime_id=old_service.owner_runtime_id,
                turn_id=new_id("turn"),
                user_message_id=new_id("message"),
                user_message_content=request.message,
            )
        )

        new_runtime = _quota_runtime(settings, stream_orphan_recovery_limit=1)
        new_service = new_runtime.agent_stream_service
        assert new_service is not None
        events = _run_handle(
            new_service.stream_chat(
                user_context=_context(new_runtime, "req_legacy_replay"),
                request=request,
            )
        )

        assert isinstance(events[-1], TurnFailed)
        assert _stream_row(new_runtime, turn_id=events[-1].turn_id)["terminal_reason"] == (
            "runtime_orphaned"
        )
        assert _quota_reservation_count(new_runtime) == 0
    finally:
        if new_runtime is not None:
            new_runtime.close()
        old_runtime.close()


def test_quota_preflight_failure_logs_released_terminal(settings) -> None:
    runtime = _quota_runtime(settings)
    try:
        service = runtime.agent_stream_service
        assert service is not None
        logger = _CaptureLogger()
        service.app_logger = logger
        session, request = _session_request(runtime, key="cm-quota-preflight-log")

        def _fail_preflight(**kwargs):
            del kwargs
            raise AemeathError(
                code=ErrorCode.SESSION_ARCHIVED,
                public_message="Session is archived and cannot be used.",
                internal_message="forced_preflight_failure",
                retryable=False,
            )

        service.turn_engine.preflight = _fail_preflight

        with pytest.raises(AemeathError) as raised:
            service.stream_chat(
                user_context=_context(runtime, "req_preflight_log"),
                request=request,
            )
        assert raised.value.code == ErrorCode.SESSION_ARCHIVED

        assert any(
            record.event_name == "usage_quota.released"
            and record.error_code == ErrorCode.SESSION_ARCHIVED.value
            for record in logger.records
        )
    finally:
        runtime.close()


def test_request_id_does_not_change_fingerprint_and_business_change_conflicts(
    stream_runtime,
) -> None:
    runtime = stream_runtime
    service = runtime.agent_stream_service
    assert service is not None
    session, request = _session_request(
        runtime,
        key="cm-idempotent",
        message="same business payload",
        metadata={"nested": [1, "二"]},
    )

    first = service.stream_chat(user_context=_context(runtime, "req_A"), request=request)
    first_events = _run_handle(first)
    model = service.model_client.delegate
    assert model.open_count == 1

    replay = service.stream_chat(user_context=_context(runtime, "req_B"), request=request)
    replay_events = _run_handle(replay)
    assert model.open_count == 1
    assert replay_events[0].request_id == "req_B"
    assert [event.event_type for event in first_events] == [
        "turn.started",
        "response.delta",
        "turn.completed",
    ]
    assert [event.event_type for event in replay_events] == [
        "turn.started",
        "response.delta",
        "turn.completed",
    ]

    with pytest.raises(AemeathError) as raised:
        service.stream_chat(
            user_context=_context(runtime, "req_C"),
            request=replace(request, message="changed business payload"),
        )
    assert raised.value.code == ErrorCode.CHAT_REQUEST_CONFLICT
    assert model.open_count == 1
    roles = [
        item.role
        for item in runtime.store.list_messages_by_session(session_id=session.id)
    ]
    assert roles == [
        "user",
        "assistant",
    ]


def test_live_in_progress_is_rejected_without_provider_call(stream_runtime) -> None:
    runtime = stream_runtime
    service = runtime.agent_stream_service
    registry = runtime.active_stream_registry
    assert service is not None and registry is not None
    session, request = _session_request(runtime, key="cm-live")
    digest = fingerprint_chat_request_v1(
        character_id=request.character_id,
        message=request.message,
        metadata=request.metadata,
    )
    reserved = runtime.store.resolve_or_reserve(
        request=StreamTurnResolveOrReserve(
            user_id=runtime.settings.default_user_id,
            session_id=session.id,
            active_character_id="aemeath",
            client_message_id=request.client_message_id,
            request_fingerprint_version=1,
            request_fingerprint=digest,
            owner_runtime_id=service.owner_runtime_id,
            turn_id=new_id("turn"),
            user_message_id=new_id("message"),
            user_message_content=request.message,
        )
    )
    live = _LiveHandle(reserved.record.turn_id)
    registry.register(live)
    try:
        with pytest.raises(AemeathError) as raised:
            service.stream_chat(user_context=_context(runtime, "req_live"), request=request)
        assert raised.value.code == ErrorCode.CHAT_REQUEST_IN_PROGRESS
        assert service.model_client.delegate.open_count == 0
    finally:
        registry.deregister(handle_id=live.handle_id)


def test_current_runtime_orphan_is_failed_and_replayed_without_provider(
    stream_runtime,
) -> None:
    runtime = stream_runtime
    service = runtime.agent_stream_service
    assert service is not None
    session, request = _session_request(runtime, key="cm-local-orphan")
    runtime.store.resolve_or_reserve(
        request=StreamTurnResolveOrReserve(
            user_id=runtime.settings.default_user_id,
            session_id=session.id,
            active_character_id="aemeath",
            client_message_id=request.client_message_id,
            request_fingerprint_version=1,
            request_fingerprint=fingerprint_chat_request_v1(
                character_id=request.character_id,
                message=request.message,
                metadata=request.metadata,
            ),
            owner_runtime_id=service.owner_runtime_id,
            turn_id=new_id("turn"),
            user_message_id=new_id("message"),
            user_message_content=request.message,
        )
    )

    handle = service.stream_chat(
        user_context=_context(runtime, "req_local_orphan"),
        request=request,
    )
    terminal = _run_handle(handle)[-1]

    assert isinstance(terminal, TurnFailed)
    assert terminal.error.code == ErrorCode.CHAT_STREAM_INTERRUPTED.value
    assert service.model_client.delegate.open_count == 0


@pytest.mark.parametrize(
    ("events", "expected_terminal", "expected_warning"),
    [
        ((ModelStreamFinished("complete"),), TurnFailed, None),
        (
            (ModelTextDelta("truncated answer"), ModelStreamFinished("output_limit")),
            TurnCompleted,
            "model_output_truncated",
        ),
    ],
)
def test_empty_answer_fails_and_output_limit_completes_with_warning(
    stream_runtime,
    events,
    expected_terminal,
    expected_warning,
) -> None:
    runtime = stream_runtime
    service = runtime.agent_stream_service
    assert service is not None
    service.model_client = MockStreamingChatModelClient(events=events)
    session, request = _session_request(runtime, key=new_id("cm"))
    handle = service.stream_chat(user_context=_context(runtime, new_id("req")), request=request)
    emitted = _run_handle(handle)
    terminal = emitted[-1]
    assert isinstance(terminal, expected_terminal)
    messages = runtime.store.list_messages_by_session(session_id=session.id)
    if expected_warning is None:
        assert [item.role for item in messages] == ["user"]
        assert terminal.error.code == ErrorCode.MODEL_INVALID_RESPONSE.value
    else:
        assert [item.role for item in messages] == ["user", "assistant"]
        assert terminal.warnings == (expected_warning,)


def test_provider_partial_failure_persists_safe_reason_and_no_partial(stream_runtime) -> None:
    runtime = stream_runtime
    service = runtime.agent_stream_service
    assert service is not None
    provider_secret = "RAW_PROVIDER_FAILURE_07c9"
    service.model_client = MockStreamingChatModelClient(
        events=(ModelTextDelta("partial secret response"),),
        failure=AemeathError(
            code=ErrorCode.MODEL_TIMEOUT,
            public_message="The model request timed out. Please try again.",
            internal_message=provider_secret,
            retryable=True,
        ),
        fail_after_events=1,
    )
    session, request = _session_request(runtime, key="cm-provider-fail")
    handle = service.stream_chat(user_context=_context(runtime, "req_provider"), request=request)
    emitted = _run_handle(handle)

    assert isinstance(emitted[-1], TurnFailed)
    assert emitted[-1].error.code == ErrorCode.MODEL_TIMEOUT.value
    roles = [
        item.role
        for item in runtime.store.list_messages_by_session(session_id=session.id)
    ]
    assert roles == [
        "user"
    ]
    replay = runtime.store.resolve_or_reserve(
        request=StreamTurnResolveOrReserve(
            user_id=runtime.settings.default_user_id,
            session_id=session.id,
            active_character_id="aemeath",
            client_message_id=request.client_message_id,
            request_fingerprint_version=1,
            request_fingerprint=fingerprint_chat_request_v1(
                character_id=request.character_id,
                message=request.message,
                metadata=request.metadata,
            ),
            owner_runtime_id=service.owner_runtime_id,
            turn_id=new_id("turn"),
            user_message_id=new_id("message"),
            user_message_content=request.message,
        )
    )
    assert replay.record.terminal_reason == "model_failed"
    assert provider_secret not in repr(replay.record)
    replay_events = _run_handle(
        service.stream_chat(
            user_context=_context(runtime, "req_provider_replay"),
            request=request,
        )
    )
    assert isinstance(replay_events[-1], TurnFailed)
    assert service.model_client.open_count == 1


def test_consumer_slow_cancels_and_releases_all_runtime_resources(stream_runtime) -> None:
    runtime = stream_runtime
    service = runtime.agent_stream_service
    registry = runtime.active_stream_registry
    assert service is not None and registry is not None
    session, request = _session_request(runtime, key="cm-slow")
    sink = _RecordingSink(slow_once=True)
    handle = service.stream_chat(user_context=_context(runtime, "req_slow"), request=request)
    handle.start(sink=sink)
    assert sink.closed.wait(timeout=3)
    handle.close()

    assert isinstance(sink.events[-1], TurnFailed)
    assert sink.events[-1].error.code == ErrorCode.CHAT_STREAM_CANCELLED.value
    roles = [
        item.role
        for item in runtime.store.list_messages_by_session(session_id=session.id)
    ]
    assert roles == [
        "user"
    ]
    assert service.admission.entry_count == 0
    assert registry.active_count == 0


@pytest.mark.parametrize(
    "delivery_error",
    [
        TimeoutError("bounded replay sink"),
        RuntimeError("closed replay sink"),
    ],
)
def test_completed_replay_delivery_failure_never_synthesizes_failed_terminal(
    stream_runtime,
    delivery_error,
) -> None:
    runtime = stream_runtime
    service = runtime.agent_stream_service
    assert service is not None
    _session, request = _session_request(runtime, key=f"cm-replay-{type(delivery_error).__name__}")
    assert isinstance(
        _run_handle(
            service.stream_chat(user_context=_context(runtime, "req_original"), request=request)
        )[-1],
        TurnCompleted,
    )
    model = service.model_client.delegate
    assert model.open_count == 1

    sink = _RejectingSink(error=delivery_error)
    replay = service.stream_chat(user_context=_context(runtime, "req_replay"), request=request)
    replay.start(sink=sink)
    assert sink.closed.wait(timeout=3)
    replay.close()

    assert not any(isinstance(event, TurnFailed) for event in sink.events)
    assert _resolve_existing(runtime, service, request).kind == "replay_completed"
    healthy_replay = service.stream_chat(
        user_context=_context(runtime, "req_healthy_replay"),
        request=request,
    )
    assert isinstance(_run_handle(healthy_replay)[-1], TurnCompleted)
    assert model.open_count == 1
    assert service.admission.entry_count == 0


def test_completed_replay_cancel_preserves_durable_completed_terminal(stream_runtime) -> None:
    runtime = stream_runtime
    service = runtime.agent_stream_service
    assert service is not None
    _session, request = _session_request(runtime, key="cm-replay-completed-cancel")
    assert isinstance(
        _run_handle(
            service.stream_chat(user_context=_context(runtime, "req_original"), request=request)
        )[-1],
        TurnCompleted,
    )
    model = service.model_client.delegate

    replay = service.stream_chat(user_context=_context(runtime, "req_cancel"), request=request)
    replay.cancel(reason="client_disconnected")
    sink = _RecordingSink()
    replay.start(sink=sink)
    assert sink.closed.wait(timeout=3)
    replay.close()

    assert sink.events == []
    assert _resolve_existing(runtime, service, request).kind == "replay_completed"
    assert isinstance(
        _run_handle(
            service.stream_chat(
                user_context=_context(runtime, "req_after_cancel"),
                request=request,
            )
        )[-1],
        TurnCompleted,
    )
    assert model.open_count == 1


@pytest.mark.parametrize(
    "delivery_error",
    [
        StreamConsumerSlowError("bounded replay sink"),
        RuntimeError("closed replay sink"),
    ],
)
def test_failed_replay_delivery_failure_preserves_original_failed_terminal(
    stream_runtime,
    delivery_error,
) -> None:
    runtime = stream_runtime
    service = runtime.agent_stream_service
    assert service is not None
    _session, request = _session_request(runtime, key=f"cm-failed-{type(delivery_error).__name__}")
    original = service.stream_chat(user_context=_context(runtime, "req_original"), request=request)
    original.close()
    durable = _resolve_existing(runtime, service, request)
    assert durable.kind == "replay_failed"
    original_code = durable.record.terminal_error_code
    model = service.model_client.delegate
    assert model.open_count == 0

    sink = _RejectingSink(error=delivery_error)
    replay = service.stream_chat(user_context=_context(runtime, "req_replay"), request=request)
    replay.start(sink=sink)
    assert sink.closed.wait(timeout=3)
    replay.close()

    assert sink.events == []
    durable_after = _resolve_existing(runtime, service, request)
    assert durable_after.kind == "replay_failed"
    assert durable_after.record.terminal_error_code == original_code
    healthy_terminal = _run_handle(
        service.stream_chat(
            user_context=_context(runtime, "req_healthy_replay"),
            request=request,
        )
    )[-1]
    assert isinstance(healthy_terminal, TurnFailed)
    assert healthy_terminal.error.code == original_code
    assert model.open_count == 0


def test_failed_replay_cancel_preserves_original_failed_terminal(stream_runtime) -> None:
    runtime = stream_runtime
    service = runtime.agent_stream_service
    assert service is not None
    _session, request = _session_request(runtime, key="cm-replay-failed-cancel")
    original = service.stream_chat(user_context=_context(runtime, "req_original"), request=request)
    original.close()
    durable = _resolve_existing(runtime, service, request)
    assert durable.kind == "replay_failed"
    original_code = durable.record.terminal_error_code

    replay = service.stream_chat(user_context=_context(runtime, "req_cancel"), request=request)
    replay.cancel(reason="client_disconnected")
    sink = _RecordingSink()
    replay.start(sink=sink)
    assert sink.closed.wait(timeout=3)
    replay.close()

    assert sink.events == []
    durable_after = _resolve_existing(runtime, service, request)
    assert durable_after.kind == "replay_failed"
    assert durable_after.record.terminal_error_code == original_code
    terminal = _run_handle(
        service.stream_chat(
            user_context=_context(runtime, "req_after_cancel"),
            request=request,
        )
    )[-1]
    assert isinstance(terminal, TurnFailed)
    assert terminal.error.code == original_code


def test_cancel_wins_before_completion_and_partial_is_not_persisted(stream_runtime) -> None:
    runtime = stream_runtime
    service = runtime.agent_stream_service
    assert service is not None
    blocking = _BlockingStreamingClient()
    service.model_client.delegate = blocking
    session, request = _session_request(runtime, key="cm-cancel")
    sink = _RecordingSink()
    handle = service.stream_chat(user_context=_context(runtime, "req_cancel"), request=request)
    handle.start(sink=sink)
    assert blocking.session.entered.wait(timeout=2)
    handle.cancel(reason="user_cancelled")
    assert sink.closed.wait(timeout=3)
    handle.close()

    terminals = [event for event in sink.events if isinstance(event, (TurnCompleted, TurnFailed))]
    assert len(terminals) == 1
    assert isinstance(terminals[0], TurnFailed)
    assert terminals[0].error.retryable is False
    assert blocking.session.close_count == 1
    roles = [
        item.role
        for item in runtime.store.list_messages_by_session(session_id=session.id)
    ]
    assert roles == [
        "user"
    ]
    assert service.model_client.gate.counts()[0] == 0
    assert service.admission.entry_count == 0


def test_completion_commit_wins_cancel_race_and_terminal_is_unique(
    stream_runtime,
    monkeypatch,
) -> None:
    runtime = stream_runtime
    service = runtime.agent_stream_service
    assert service is not None
    session, request = _session_request(runtime, key="cm-complete-race")
    entered_complete = Event()
    release_complete = Event()
    original_complete = runtime.store.complete

    def blocked_complete(*, request):
        entered_complete.set()
        release_complete.wait(timeout=2)
        return original_complete(request=request)

    monkeypatch.setattr(runtime.store, "complete", blocked_complete)
    sink = _RecordingSink()
    handle = service.stream_chat(user_context=_context(runtime, "req_race"), request=request)
    handle.start(sink=sink)
    assert entered_complete.wait(timeout=2)
    cancel_thread = Thread(target=handle.cancel, kwargs={"reason": "user_cancelled"})
    cancel_thread.start()
    release_complete.set()
    cancel_thread.join(timeout=1)
    assert sink.closed.wait(timeout=3)
    handle.close()

    terminals = [event for event in sink.events if isinstance(event, (TurnCompleted, TurnFailed))]
    assert len(terminals) == 1
    assert isinstance(terminals[0], TurnCompleted)
    roles = [
        item.role
        for item in runtime.store.list_messages_by_session(session_id=session.id)
    ]
    assert roles == [
        "user",
        "assistant",
    ]


def test_runtime_shutdown_cancels_active_releases_gate_and_rejects_new(stream_runtime) -> None:
    runtime = stream_runtime
    service = runtime.agent_stream_service
    registry = runtime.active_stream_registry
    assert service is not None and registry is not None
    blocking = _BlockingStreamingClient()
    service.model_client.delegate = blocking
    _session, request = _session_request(runtime, key="cm-shutdown")
    sink = _RecordingSink()
    handle = service.stream_chat(user_context=_context(runtime, "req_shutdown"), request=request)
    handle.start(sink=sink)
    assert blocking.session.entered.wait(timeout=2)

    runtime.close()

    assert sink.closed.wait(timeout=2)
    assert registry.active_count == 0
    assert service.model_client.gate.counts()[0] == 0
    assert service.admission.entry_count == 0
    assert blocking.session.close_count == 1
    with pytest.raises(RuntimeBusyError, match="stream_registry_closing"):
        service.stream_chat(user_context=_context(runtime, "req_after"), request=request)


def test_registry_close_during_registration_terminalizes_reserved_turn(
    stream_runtime,
    monkeypatch,
) -> None:
    runtime = stream_runtime
    service = runtime.agent_stream_service
    registry = runtime.active_stream_registry
    assert service is not None and registry is not None
    _session, request = _session_request(runtime, key="cm-register-close-race")
    trace = _CaptureLogger()
    service.turn_engine.trace_logger = trace
    register_entered = Event()
    release_register = Event()
    original_register = registry.register

    def blocked_register(handle):
        register_entered.set()
        release_register.wait(timeout=2)
        return original_register(handle)

    monkeypatch.setattr(registry, "register", blocked_register)
    result: dict[str, Any] = {}

    def create_handle() -> None:
        try:
            result["handle"] = service.stream_chat(
                user_context=_context(runtime, "req_register_race"),
                request=request,
            )
        except BaseException as exc:
            result["error"] = exc

    creator = Thread(target=create_handle)
    creator.start()
    assert register_entered.wait(timeout=2)
    registry.close()
    release_register.set()
    creator.join(timeout=3)

    assert not creator.is_alive()
    assert isinstance(result.get("error"), RuntimeBusyError)
    assert "handle" not in result
    assert _resolve_existing(runtime, service, request).kind == "replay_failed"
    assert service.model_client.delegate.open_count == 0
    assert service.model_client.gate.counts()[0] == 0
    assert service.admission.entry_count == 0
    assert registry.active_count == 0
    trace_types = [record.event_type for record in trace.records]
    assert trace_types.index("turn_started") < trace_types.index("turn_failed")


def test_same_session_different_key_is_serialized_without_second_provider(
    stream_runtime,
) -> None:
    runtime = stream_runtime
    service = runtime.agent_stream_service
    assert service is not None
    blocking = _BlockingStreamingClient()
    service.model_client.delegate = blocking
    _session, first_request = _session_request(runtime, key="cm-serial-first")
    second_request = replace(first_request, client_message_id="cm-serial-second")
    sink = _RecordingSink()
    first = service.stream_chat(
        user_context=_context(runtime, "req_serial_first"),
        request=first_request,
    )
    first.start(sink=sink)
    assert blocking.session.entered.wait(timeout=2)

    with pytest.raises(RuntimeBusyError) as raised:
        service.stream_chat(
            user_context=_context(runtime, "req_serial_second"),
            request=second_request,
        )
    assert raised.value.reason == "session_lock_wait_timeout"
    assert blocking.open_count == 1
    first.cancel(reason="user_cancelled")
    assert sink.closed.wait(timeout=3)
    first.close()


def test_ref_counted_key_registry_is_fixed_at_128_and_cleans_up() -> None:
    registry = RefCountedStreamKeyRegistry(wait_timeout_seconds=0)
    managers = []
    try:
        for index in range(128):
            manager = registry.acquire(key=("user", f"session-{index}", "cm"))
            manager.__enter__()
            managers.append(manager)
        assert registry.entry_count == 128
        with pytest.raises(RuntimeBusyError, match="stream_key_registry_capacity"):
            with registry.acquire(key=("user", "session-overflow", "cm")):
                pass
    finally:
        for manager in reversed(managers):
            manager.__exit__(None, None, None)
    assert registry.entry_count == 0


def test_ended_allows_terminal_replay_but_archived_and_new_turn_are_rejected(
    stream_runtime,
) -> None:
    runtime = stream_runtime
    service = runtime.agent_stream_service
    assert service is not None
    session, request = _session_request(runtime, key="cm-lifecycle")
    first = service.stream_chat(user_context=_context(runtime, "req_first"), request=request)
    assert isinstance(_run_handle(first)[-1], TurnCompleted)
    runtime.store.end_session(
        session_id=session.id,
        user_id=runtime.settings.default_user_id,
    )

    replay = service.stream_chat(user_context=_context(runtime, "req_replay"), request=request)
    assert isinstance(_run_handle(replay)[-1], TurnCompleted)
    with pytest.raises(SessionEndedError):
        service.stream_chat(
            user_context=_context(runtime, "req_new"),
            request=replace(request, client_message_id="cm-new-ended"),
        )

    runtime.store.archive_session(
        session_id=session.id,
        user_id=runtime.settings.default_user_id,
    )
    with pytest.raises(SessionArchivedError):
        service.stream_chat(user_context=_context(runtime, "req_archived"), request=request)


def test_cross_user_cannot_resolve_existing_stream_record(stream_runtime) -> None:
    runtime = stream_runtime
    service = runtime.agent_stream_service
    assert service is not None
    _session, request = _session_request(runtime, key="cm-owner")
    first = service.stream_chat(user_context=_context(runtime, "req_owner"), request=request)
    assert isinstance(_run_handle(first)[-1], TurnCompleted)

    with pytest.raises(SessionOwnershipError):
        service.stream_chat(
            user_context=UserContext(user_id="different_user", request_id="req_other"),
            request=request,
        )


def test_local_output_limits_fail_as_model_invalid_without_saving_partial(
    stream_runtime,
) -> None:
    runtime = stream_runtime
    service = runtime.agent_stream_service
    assert service is not None
    service.limits = StreamServiceLimits(
        response_max_chars=3,
        response_max_utf8_bytes=12,
        public_delta_max_utf8_bytes=4,
        producer_put_timeout_seconds=0.1,
        producer_join_timeout_seconds=1,
    )
    service.model_client = MockStreamingChatModelClient(
        events=(ModelTextDelta("four"), ModelStreamFinished("complete"))
    )
    session, request = _session_request(runtime, key="cm-output-limit")

    handle = service.stream_chat(user_context=_context(runtime, "req_limit"), request=request)
    terminal = _run_handle(handle)[-1]

    assert isinstance(terminal, TurnFailed)
    assert terminal.error.code == ErrorCode.MODEL_INVALID_RESPONSE.value
    roles = [
        item.role
        for item in runtime.store.list_messages_by_session(session_id=session.id)
    ]
    assert roles == ["user"]


def test_public_delta_split_preserves_unicode_and_byte_limit(stream_runtime) -> None:
    runtime = stream_runtime
    service = runtime.agent_stream_service
    assert service is not None
    service.limits = StreamServiceLimits(
        response_max_chars=100,
        response_max_utf8_bytes=100,
        public_delta_max_utf8_bytes=4,
        producer_put_timeout_seconds=0.1,
        producer_join_timeout_seconds=1,
    )
    answer = "\u4f60ab\u597d"
    service.model_client = MockStreamingChatModelClient(
        events=(ModelTextDelta(answer), ModelStreamFinished("complete"))
    )
    _session, request = _session_request(runtime, key="cm-unicode-split")

    events = _run_handle(
        service.stream_chat(user_context=_context(runtime, "req_split"), request=request)
    )
    deltas = [event.delta for event in events if isinstance(event, ResponseDelta)]

    assert "".join(deltas) == answer
    assert all(len(delta.encode("utf-8")) <= 4 for delta in deltas)


def test_post_commit_memory_failure_cannot_reverse_completed(
    stream_runtime,
    monkeypatch,
) -> None:
    runtime = stream_runtime
    service = runtime.agent_stream_service
    assert service is not None
    session, request = _session_request(runtime, key="cm-memory-fault")
    admission_counts = []

    def fail_memory(**_kwargs):
        admission_counts.append(service.admission.entry_count)
        raise RuntimeError("PRIVATE_MEMORY_FAILURE_BODY")

    monkeypatch.setattr(service.turn_engine.memory_writer, "persist_decision", fail_memory)
    terminal = _run_handle(
        service.stream_chat(user_context=_context(runtime, "req_memory"), request=request)
    )[-1]

    assert isinstance(terminal, TurnCompleted)
    assert admission_counts == [0]
    roles = [
        item.role
        for item in runtime.store.list_messages_by_session(session_id=session.id)
    ]
    assert roles == ["user", "assistant"]


def test_handle_start_cancel_close_are_repeatable_and_thread_safe(stream_runtime) -> None:
    runtime = stream_runtime
    service = runtime.agent_stream_service
    assert service is not None
    _session, request = _session_request(runtime, key="cm-handle-repeat")
    handle = service.stream_chat(user_context=_context(runtime, "req_handle"), request=request)
    sink = _RecordingSink()
    handle.start(sink=sink)
    with pytest.raises(RuntimeError, match="already_started_or_closed"):
        handle.start(sink=sink)
    assert sink.closed.wait(timeout=3)

    threads = [Thread(target=handle.close) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=1)
    handle.cancel(reason="user_cancelled")
    handle.cancel(reason="user_cancelled")

    assert all(not thread.is_alive() for thread in threads)
    assert service.admission.entry_count == 0


def test_close_cannot_observe_producer_before_thread_start(
    stream_runtime,
    monkeypatch,
) -> None:
    runtime = stream_runtime
    service = runtime.agent_stream_service
    assert service is not None
    _session, request = _session_request(runtime, key="cm-thread-start-race")
    handle = service.stream_chat(
        user_context=_context(runtime, "req_thread_start_race"),
        request=request,
    )
    sink = _RecordingSink()
    start_entered = Event()
    release_start = Event()

    class _BlockedStartThread(Thread):
        def start(self) -> None:
            start_entered.set()
            release_start.wait(timeout=2)
            super().start()

    monkeypatch.setattr(agent_stream_service_module, "Thread", _BlockedStartThread)
    errors: list[BaseException] = []

    def start_handle() -> None:
        try:
            handle.start(sink=sink)
        except BaseException as exc:
            errors.append(exc)

    def close_handle() -> None:
        try:
            handle.close()
        except BaseException as exc:
            errors.append(exc)

    starter = Thread(target=start_handle)
    closer = Thread(target=close_handle)
    starter.start()
    assert start_entered.wait(timeout=2)
    closer.start()
    release_start.set()
    starter.join(timeout=3)
    closer.join(timeout=3)
    assert sink.closed.wait(timeout=3)
    handle.close()

    assert not starter.is_alive()
    assert not closer.is_alive()
    assert errors == []
    assert sink.put_after_close is False
    terminals = [event for event in sink.events if isinstance(event, (TurnCompleted, TurnFailed))]
    assert len(terminals) == 1
    assert service.admission.entry_count == 0
    assert runtime.active_stream_registry.active_count == 0


def test_close_from_created_blocks_later_start_before_persistence_work(
    stream_runtime,
    monkeypatch,
) -> None:
    runtime = stream_runtime
    service = runtime.agent_stream_service
    assert service is not None
    _session, request = _session_request(runtime, key="cm-created-close-race")
    handle = service.stream_chat(
        user_context=_context(runtime, "req_created_close_race"),
        request=request,
    )
    fail_entered = Event()
    release_fail = Event()
    original_fail = runtime.store.fail

    def blocked_fail(*, request):
        fail_entered.set()
        release_fail.wait(timeout=2)
        return original_fail(request=request)

    monkeypatch.setattr(runtime.store, "fail", blocked_fail)
    close_errors: list[BaseException] = []

    def close_handle() -> None:
        try:
            handle.close()
        except BaseException as exc:
            close_errors.append(exc)

    closer = Thread(target=close_handle)
    closer.start()
    assert fail_entered.wait(timeout=2)
    sink = _RecordingSink()
    with pytest.raises(RuntimeError, match="already_started_or_closed"):
        handle.start(sink=sink)
    release_fail.set()
    closer.join(timeout=3)
    handle.close()

    assert not closer.is_alive()
    assert close_errors == []
    assert sink.events == []
    assert sink.closed.is_set() is False
    assert service.model_client.delegate.open_count == 0
    assert _resolve_existing(runtime, service, request).kind == "replay_failed"
    assert service.admission.entry_count == 0
    assert runtime.active_stream_registry.active_count == 0


def test_close_before_start_fails_reserved_turn_without_provider(stream_runtime) -> None:
    runtime = stream_runtime
    service = runtime.agent_stream_service
    assert service is not None
    session, request = _session_request(runtime, key="cm-never-started")
    model = service.model_client.delegate
    handle = service.stream_chat(
        user_context=_context(runtime, "req_never_started"),
        request=request,
    )

    handle.close()
    handle.close()

    assert model.open_count == 0
    roles = [
        item.role
        for item in runtime.store.list_messages_by_session(session_id=session.id)
    ]
    assert roles == ["user"]
    replay = service.stream_chat(
        user_context=_context(runtime, "req_never_started_replay"),
        request=request,
    )
    assert isinstance(_run_handle(replay)[-1], TurnFailed)
    assert model.open_count == 0


def test_model_invalid_response_persists_provider_neutral_reason(stream_runtime) -> None:
    runtime = stream_runtime
    service = runtime.agent_stream_service
    assert service is not None
    service.model_client = MockStreamingChatModelClient(
        failure=AemeathError(
            code=ErrorCode.MODEL_INVALID_RESPONSE,
            public_message="The model returned an invalid response.",
            internal_message="deepseek_private_parser_reason",
            retryable=False,
        ),
        fail_after_events=0,
    )
    _session, request = _session_request(runtime, key="cm-invalid-model")
    handle = service.stream_chat(
        user_context=_context(runtime, "req_invalid_model"),
        request=request,
    )
    assert isinstance(_run_handle(handle)[-1], TurnFailed)

    outcome = runtime.store.resolve_or_reserve(
        request=StreamTurnResolveOrReserve(
            user_id=runtime.settings.default_user_id,
            session_id=request.session_id,
            active_character_id="aemeath",
            client_message_id=request.client_message_id,
            request_fingerprint_version=1,
            request_fingerprint=fingerprint_chat_request_v1(
                character_id=request.character_id,
                message=request.message,
                metadata=request.metadata,
            ),
            owner_runtime_id=service.owner_runtime_id,
            turn_id=new_id("turn"),
            user_message_id=new_id("message"),
            user_message_content=request.message,
        )
    )
    assert outcome.record.terminal_reason == "model_invalid_response"
    assert "deepseek" not in repr(outcome.record)


def test_stream_logs_prompt_summary_errors_and_repr_do_not_leak_content(
    stream_runtime,
) -> None:
    runtime = stream_runtime
    service = runtime.agent_stream_service
    assert service is not None
    user_marker = "PRIVATE_USER_PROMPT_MARKER_51af"
    response_marker = "PRIVATE_RESPONSE_DELTA_MARKER_8c31"
    credential_marker = "PRIVATE_TOKEN_MARKER_791e"
    trace = _CaptureLogger()
    prompt = _CaptureLogger()
    app = _CaptureLogger()
    service.turn_engine.trace_logger = trace
    service.turn_engine.prompt_run_logger = prompt
    service.app_logger = app
    service.model_client = MockStreamingChatModelClient(
        events=(ModelTextDelta(response_marker), ModelStreamFinished("complete"))
    )
    _session, request = _session_request(
        runtime,
        key="cm-zero-leak",
        message=user_marker,
        metadata={
            "credential": credential_marker,
            "context_budget": {"status": credential_marker, "secret_count": 7},
        },
    )

    handle = service.stream_chat(
        user_context=_context(runtime, "req_zero_leak"),
        request=request,
    )
    events = _run_handle(handle)
    captured_repr = repr(
        {
            "handle": handle,
            "events": events,
            "trace": trace.records,
            "prompt": prompt.records,
            "app": app.records,
        }
    )

    assert user_marker not in captured_repr
    assert response_marker not in captured_repr
    assert credential_marker not in captured_repr
    assert prompt.records
    assert prompt.records[-1].messages == []
    assert prompt.records[-1].sections == []
    assert prompt.records[-1].response is None


def test_failed_stream_observability_does_not_leak_raw_content_or_exception_body(
    stream_runtime,
) -> None:
    runtime = stream_runtime
    service = runtime.agent_stream_service
    assert service is not None
    user_marker = "PRIVATE_FAILED_USER_MARKER_d4a1"
    delta_marker = "PRIVATE_FAILED_DELTA_MARKER_3fa2"
    credential_marker = "PRIVATE_FAILED_CREDENTIAL_MARKER_c7b3"
    provider_marker = "PRIVATE_PROVIDER_RAW_RESPONSE_MARKER_0d94"
    exception_marker = "PRIVATE_EXCEPTION_BODY_MARKER_b2e5"
    vector_marker = "PRIVATE_VECTOR_MARKER_8ac6"
    trace = _CaptureLogger()
    prompt = _CaptureLogger()
    app = _CaptureLogger()
    service.turn_engine.trace_logger = trace
    service.turn_engine.prompt_run_logger = prompt
    service.app_logger = app
    service.model_client = MockStreamingChatModelClient(
        events=(ModelTextDelta(delta_marker),),
        failure=RuntimeError(
            f"provider body={provider_marker} exception={exception_marker} "
            f"authorization=Bearer {credential_marker}"
        ),
        fail_after_events=1,
    )
    _session, request = _session_request(
        runtime,
        key="cm-failed-zero-leak",
        message=user_marker,
        metadata={
            "credential": credential_marker,
            "embedding_vector": [vector_marker],
        },
    )

    handle = service.stream_chat(
        user_context=_context(runtime, "req_failed_zero_leak"),
        request=request,
    )
    events = _run_handle(handle)
    captured_logs = repr(
        {
            "trace": trace.records,
            "prompt": prompt.records,
            "app": app.records,
        }
    )

    assert isinstance(events[-1], TurnFailed)
    assert events[-1].error.code == ErrorCode.INTERNAL_ERROR.value
    for marker in (
        user_marker,
        delta_marker,
        credential_marker,
        provider_marker,
        exception_marker,
        vector_marker,
    ):
        assert marker not in captured_logs
        assert marker not in repr(events[-1])
    assert prompt.records
    assert prompt.records[-1].status == "failed"
    assert prompt.records[-1].messages == []
    assert prompt.records[-1].sections == []
    assert prompt.records[-1].response is None
    assert prompt.records[-1].error is None


def test_api_runtime_startup_recovers_foreign_orphan_in_bounded_batch(settings) -> None:
    first = build_application_runtime(settings=settings, surface="cli")
    session = first.store.create_session(
        user_id=settings.default_user_id,
        character_id=settings.default_character_id,
    )
    request = StreamChatRequest(
        session_id=session.id,
        character_id="aemeath",
        message="orphaned request",
        client_message_id="cm-startup-orphan",
        metadata={},
    )
    digest = fingerprint_chat_request_v1(
        character_id=request.character_id,
        message=request.message,
        metadata=request.metadata,
    )
    first.store.resolve_or_reserve(
        request=StreamTurnResolveOrReserve(
            user_id=settings.default_user_id,
            session_id=session.id,
            active_character_id="aemeath",
            client_message_id=request.client_message_id,
            request_fingerprint_version=1,
            request_fingerprint=digest,
            owner_runtime_id="runtime_previous_process",
            turn_id=new_id("turn"),
            user_message_id=new_id("message"),
            user_message_content=request.message,
        )
    )
    first.close()

    runtime = build_application_runtime(settings=settings, surface="api")
    try:
        service = runtime.agent_stream_service
        assert service is not None
        handle = service.stream_chat(
            user_context=UserContext(
                user_id=settings.default_user_id,
                request_id="req_orphan_replay",
            ),
            request=request,
        )
        terminal = _run_handle(handle)[-1]
        assert isinstance(terminal, TurnFailed)
        assert terminal.error.code == ErrorCode.CHAT_STREAM_INTERRUPTED.value
        assert terminal.error.retryable is False
    finally:
        runtime.close()


def test_s4_domain_and_runtime_modules_have_no_http_or_sse_adapter_imports() -> None:
    source = "\n".join(
        Path(path).read_text(encoding="utf-8")
        for path in (
            "app/services/agent_stream_service.py",
            "app/services/stream_schemas.py",
            "app/runtime/streaming.py",
        )
    )

    assert "FastAPI" not in source
    assert "StreamingResponse" not in source
    assert "text/event-stream" not in source
    assert "AsyncStreamBridge" not in source
    assert "class StreamingDialogueController" not in source
