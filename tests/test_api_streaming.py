from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from dataclasses import replace
from threading import Event, Thread
from typing import Any, Iterator

from fastapi import Request
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect

import app.services.agent_stream_service as agent_stream_service_module
from app.api.server import create_app
from app.api.sse import (
    SSE_HEARTBEAT_FRAME,
    SSE_HEARTBEAT_SECONDS,
    STREAM_BRIDGE_CAPACITY,
    AsyncStreamBridge,
    OwnedStreamingResponse,
    encode_sse_event,
)
from app.db.stream_turns import StreamTurnResolveOrReserve
from app.errors import AemeathError, ErrorCode
from app.model.mock_streaming import MockStreamingChatModelClient
from app.model.streaming import ModelStreamFinished, ModelStreamRequest, ModelTextDelta
from app.observability.noop import NoopAppLogger
from app.services.schemas import UserContext
from app.services.stream_fingerprint import fingerprint_chat_request_v1
from app.services.stream_schemas import (
    PublicStreamError,
    ResponseDelta,
    StreamChatRequest,
    StreamConsumerSlowError,
    TurnCompleted,
    TurnFailed,
    TurnStarted,
)
from app.utils.ids import new_id

_DEV_TOKEN = "stream-dev-token"
_DEV_AUTH = {"Authorization": f"Bearer {_DEV_TOKEN}"}
_ORIGIN = "https://testserver"
_PROVENANCE = {"Origin": _ORIGIN, "Sec-Fetch-Site": "same-origin"}


def _stream_body(*, session_id: str, key: str = "cm-api-stream", message: str = "hello"):
    return {
        "session_id": session_id,
        "character_id": "aemeath",
        "message": message,
        "client_message_id": key,
        "metadata": {},
    }


def _dev_app_and_session(settings, **overrides):
    configured = replace(
        settings,
        dev_auth_token=_DEV_TOKEN,
        api_chat_user_rate_per_minute=600,
        api_chat_user_burst=20,
        api_chat_ip_rate_per_minute=600,
        api_chat_ip_burst=20,
    )
    app = create_app(replace(configured, **overrides))
    session = app.state.application_runtime.store.create_session(
        user_id=app.state.runtime.settings.dev_auth_user_id,
        character_id="aemeath",
    )
    return app, session


def _parse_sse(text: str) -> list[tuple[str, dict[str, Any]]]:
    parsed = []
    for frame in text.split("\n\n"):
        if not frame or frame.startswith(":"):
            continue
        lines = frame.splitlines()
        assert len(lines) == 2
        assert lines[0].startswith("event: ")
        assert lines[1].startswith("data: ")
        parsed.append((lines[0][7:], json.loads(lines[1][6:])))
    return parsed


def _launch_asgi(
    app,
    *,
    method: str,
    path: str,
    headers: dict[str, str] | None = None,
    body: bytes = b"",
    disconnect: asyncio.Event | None = None,
    fail_on_body_number: int | None = None,
    send_error: BaseException | None = None,
):
    response_started = asyncio.Event()
    sent: list[dict[str, Any]] = []
    request_sent = False
    body_send_count = 0
    wait_forever = asyncio.Event()

    async def receive():
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        if disconnect is None:
            await wait_forever.wait()
        else:
            await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        nonlocal body_send_count
        if message["type"] == "http.response.body":
            body_send_count += 1
            if body_send_count == fail_on_body_number:
                raise send_error or OSError("injected_send_disconnect")
        sent.append(message)
        if message["type"] == "http.response.start":
            response_started.set()

    raw_headers = [
        (name.lower().encode("latin-1"), value.encode("latin-1"))
        for name, value in (headers or {}).items()
    ]
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "root_path": "",
        "headers": raw_headers,
        "client": ("127.0.0.1", 51000),
        "server": ("testserver", 80),
    }
    return asyncio.create_task(app(scope, receive, send)), response_started, sent


def _asgi_body(messages: list[dict[str, Any]]) -> str:
    return b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    ).decode("utf-8")


def _resolve_existing(app, *, session_id: str, key: str, message: str):
    service = app.state.runtime.agent_stream_service
    user_id = app.state.runtime.settings.dev_auth_user_id
    return app.state.application_runtime.store.resolve_or_reserve(
        request=StreamTurnResolveOrReserve(
            user_id=user_id,
            session_id=session_id,
            active_character_id="aemeath",
            client_message_id=key,
            request_fingerprint_version=1,
            request_fingerprint=fingerprint_chat_request_v1(
                character_id="aemeath",
                message=message,
                metadata={},
            ),
            owner_runtime_id=service.owner_runtime_id,
            turn_id=new_id("turn"),
            user_message_id=new_id("message"),
            user_message_content=message,
        )
    )


class _BlockingModelSession:
    def __init__(self, *, block_cancel: bool = False) -> None:
        self.entered = Event()
        self.release = Event()
        self.cancelled = Event()
        self.cancel_entered = Event()
        self.cancel_release = Event()
        self.block_cancel = block_cancel
        self.cancel_reason: str | None = None
        self.close_count = 0

    def __iter__(self):
        yield ModelTextDelta("partial")
        self.entered.set()
        self.release.wait(timeout=3)
        if not self.cancelled.is_set():
            yield ModelStreamFinished("complete")

    def cancel(self, *, reason: str) -> None:
        self.cancel_reason = reason
        self.cancelled.set()
        self.cancel_entered.set()
        if self.block_cancel:
            self.cancel_release.wait(timeout=2)
        self.release.set()

    def close(self) -> None:
        self.close_count += 1
        self.release.set()


class _BlockingStreamingClient:
    def __init__(self, *, block_cancel: bool = False) -> None:
        self.session = _BlockingModelSession(block_cancel=block_cancel)
        self.open_count = 0

    @contextmanager
    def open_stream(self, *, request: ModelStreamRequest) -> Iterator[_BlockingModelSession]:
        del request
        self.open_count += 1
        try:
            yield self.session
        finally:
            self.session.close()


class _BridgePressureModelSession:
    def __init__(self) -> None:
        self.second_delta_emitted = Event()
        self.cancelled = Event()
        self.cancel_reason: str | None = None
        self.close_count = 0

    def __iter__(self):
        yield ModelTextDelta("first")
        self.second_delta_emitted.set()
        yield ModelTextDelta("second")
        yield ModelStreamFinished("complete")

    def cancel(self, *, reason: str) -> None:
        self.cancel_reason = reason
        self.cancelled.set()

    def close(self) -> None:
        self.close_count += 1


class _BridgePressureStreamingClient:
    def __init__(self) -> None:
        self.session = _BridgePressureModelSession()
        self.open_count = 0

    @contextmanager
    def open_stream(self, *, request: ModelStreamRequest):
        del request
        self.open_count += 1
        try:
            yield self.session
        finally:
            self.session.close()


class _CaptureLogger:
    def __init__(self) -> None:
        self.records: list[Any] = []

    def record(self, record: Any) -> None:
        self.records.append(record)


def test_stream_endpoint_emits_exact_order_headers_and_request_id(settings) -> None:
    app, session = _dev_app_and_session(settings)
    app.state.runtime.agent_stream_service.model_client.delegate = (
        MockStreamingChatModelClient(
            events=(
                ModelTextDelta("first"),
                ModelTextDelta(" second"),
                ModelStreamFinished("complete"),
            )
        )
    )
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/stream",
            headers={**_DEV_AUTH, "X-Request-ID": "req_stream_exact"},
            json=_stream_body(session_id=session.id),
        )

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/event-stream; charset=utf-8"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-request-id"] == "req_stream_exact"
    assert "content-length" not in response.headers
    assert "connection" not in response.headers
    events = _parse_sse(response.text)
    assert [event_type for event_type, _payload in events] == [
        "turn.started",
        "response.delta",
        "response.delta",
        "turn.completed",
    ]
    assert events[0][1]["request_id"] == "req_stream_exact"
    assert events[0][1]["session_id"] == session.id
    assert "".join(payload["delta"] for event, payload in events if event == "response.delta") == (
        "first second"
    )
    assert events[-1][1]["status"] == "completed"
    assert "id:" not in response.text
    assert "retry:" not in response.text


def test_provider_failure_after_headers_is_safe_sse_terminal(settings) -> None:
    app, session = _dev_app_and_session(settings)
    private_body = "PRIVATE_PROVIDER_BODY_TOKEN_4a91"
    app.state.runtime.agent_stream_service.model_client.delegate = (
        MockStreamingChatModelClient(
            failure=AemeathError(
                code=ErrorCode.MODEL_UNAVAILABLE,
                public_message="The model service is temporarily unavailable.",
                internal_message=private_body,
                retryable=True,
            ),
            fail_after_events=0,
        )
    )
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/stream",
            headers=_DEV_AUTH,
            json=_stream_body(session_id=session.id, key="cm-provider-failure"),
        )

    assert response.status_code == 200
    events = _parse_sse(response.text)
    assert [event_type for event_type, _payload in events] == [
        "turn.started",
        "turn.failed",
    ]
    assert events[-1][1]["error"]["code"] == ErrorCode.MODEL_UNAVAILABLE.value
    assert private_body not in response.text


def test_producer_thread_start_failure_is_off_loop_and_emits_failed_terminal(
    settings,
    monkeypatch,
) -> None:
    app, session = _dev_app_and_session(settings)
    runtime = app.state.application_runtime
    service = app.state.runtime.agent_stream_service
    registry = runtime.active_stream_registry
    fail_entered = Event()
    release_fail = Event()
    original_fail = runtime.store.fail

    class _FailingStartThread(Thread):
        def start(self) -> None:
            raise RuntimeError("injected_thread_start_failure")

    def blocked_fail(*, request):
        fail_entered.set()
        release_fail.wait(timeout=2)
        return original_fail(request=request)

    monkeypatch.setattr(agent_stream_service_module, "Thread", _FailingStartThread)
    monkeypatch.setattr(runtime.store, "fail", blocked_fail)
    key = "cm-thread-start-failure"
    message = "thread start must fail safely"
    health_during_failure_completed = Event()
    start_failure_responsiveness: list[bool] = []

    def release_blocked_failure() -> None:
        if not fail_entered.wait(timeout=2):
            start_failure_responsiveness.append(False)
            release_fail.set()
            return
        start_failure_responsiveness.append(
            health_during_failure_completed.wait(timeout=0.75)
        )
        release_fail.set()

    failure_watchdog = Thread(target=release_blocked_failure)
    failure_watchdog.start()

    async def scenario() -> list[dict[str, Any]]:
        body = json.dumps(_stream_body(session_id=session.id, key=key, message=message)).encode()
        stream_task, response_started, stream_messages = _launch_asgi(
            app,
            method="POST",
            path="/v1/chat/stream",
            headers={
                **_DEV_AUTH,
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
            },
            body=body,
        )
        await asyncio.wait_for(response_started.wait(), timeout=2)
        assert await asyncio.to_thread(fail_entered.wait, 2)
        health_task, _health_started, health_messages = _launch_asgi(
            app,
            method="GET",
            path="/health",
        )
        await asyncio.wait_for(health_task, timeout=0.5)
        assert next(
            item["status"]
            for item in health_messages
            if item["type"] == "http.response.start"
        ) == 200
        health_during_failure_completed.set()
        await asyncio.to_thread(failure_watchdog.join, 2)
        assert start_failure_responsiveness == [True]
        await asyncio.wait_for(stream_task, timeout=3)
        return stream_messages

    try:
        messages = asyncio.run(scenario())
        events = _parse_sse(_asgi_body(messages))
        assert [event_type for event_type, _payload in events] == [
            "turn.started",
            "turn.failed",
        ]
        assert service.model_client.delegate.open_count == 0
        assert _resolve_existing(
            app,
            session_id=session.id,
            key=key,
            message=message,
        ).kind == "replay_failed"
        roles = [
            item.role
            for item in runtime.store.list_messages_by_session(session_id=session.id)
        ]
        assert roles == ["user"]
        assert service.model_client.gate.counts()[0] == 0
        assert service.admission.entry_count == 0
        assert registry is not None and registry.active_count == 0
    finally:
        release_fail.set()
        runtime.close()


def test_pre_header_validation_and_idempotency_conflict_use_json_error(settings) -> None:
    app, session = _dev_app_and_session(settings)
    with TestClient(app) as client:
        invalid = client.post(
            "/v1/chat/stream",
            headers=_DEV_AUTH,
            json={"session_id": session.id, "character_id": "aemeath", "message": "hello"},
        )
        first = client.post(
            "/v1/chat/stream",
            headers=_DEV_AUTH,
            json=_stream_body(session_id=session.id, key="cm-conflict", message="first"),
        )
        conflict = client.post(
            "/v1/chat/stream",
            headers=_DEV_AUTH,
            json=_stream_body(session_id=session.id, key="cm-conflict", message="changed"),
        )

    assert invalid.status_code == 400
    assert invalid.headers["content-type"].startswith("application/json")
    assert invalid.json()["error"]["code"] == ErrorCode.INVALID_INPUT.value
    assert first.status_code == 200
    assert conflict.status_code == 409
    assert conflict.headers["content-type"].startswith("application/json")
    assert conflict.json()["error"]["code"] == ErrorCode.CHAT_REQUEST_CONFLICT.value


def test_stream_and_sync_chat_share_authenticated_rate_limit(settings) -> None:
    app, session = _dev_app_and_session(
        settings,
        api_chat_user_rate_per_minute=60,
        api_chat_user_burst=1,
    )
    with TestClient(app) as client:
        streamed = client.post(
            "/v1/chat/stream",
            headers=_DEV_AUTH,
            json=_stream_body(session_id=session.id, key="cm-shared-rate"),
        )
        limited = client.post(
            "/v1/chat",
            headers=_DEV_AUTH,
            json={
                "session_id": session.id,
                "character_id": "aemeath",
                "message": "second",
                "metadata": {},
            },
        )

    assert streamed.status_code == 200
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == ErrorCode.API_RATE_LIMITED.value
    assert int(limited.headers["retry-after"]) >= 1


def test_web_cookie_stream_requires_csrf_and_rejects_bearer_hybrid(settings) -> None:
    configured = replace(
        settings,
        api_auth_mode="web_session",
        api_allow_local_default_user=False,
        api_cors_allowed_origins=(),
        web_public_origin=_ORIGIN,
        web_auth_csrf_key=b"csrf-test-key-material-exactly-32!",
    )
    app = create_app(configured)
    with TestClient(app, base_url=_ORIGIN) as client:
        invitation = app.state.runtime.browser_auth.invitations.issue(
            username="stream-user",
            display_name="Stream User",
            roles=["user"],
        )
        accepted = client.post(
            "/v1/auth/invitations/accept",
            headers=_PROVENANCE,
            json={
                "token": invitation.raw_token,
                "password": "correct horse battery staple",
            },
        )
        csrf = client.get("/v1/auth/csrf").json()["csrf_token"]
        created = client.post(
            "/v1/sessions",
            headers={**_PROVENANCE, "X-CSRF-Token": csrf},
            json={"character_id": "aemeath"},
        )
        body = _stream_body(session_id=created.json()["id"], key="cm-cookie-stream")
        missing_csrf = client.post(
            "/v1/chat/stream",
            headers=_PROVENANCE,
            json=body,
        )
        hybrid = client.post(
            "/v1/chat/stream",
            headers={**_PROVENANCE, "Authorization": "Bearer forbidden-secret"},
            json=body,
        )
        streamed = client.post(
            "/v1/chat/stream",
            headers={**_PROVENANCE, "X-CSRF-Token": csrf},
            json=body,
        )

    assert accepted.status_code == 201
    assert missing_csrf.status_code == 403
    assert missing_csrf.json()["error"]["code"] == ErrorCode.CSRF_INVALID.value
    assert hybrid.status_code == 400
    assert hybrid.json()["error"]["code"] == ErrorCode.AUTH_CREDENTIAL_CONFLICT.value
    assert "forbidden-secret" not in hybrid.text
    assert streamed.status_code == 200
    assert _parse_sse(streamed.text)[-1][0] == "turn.completed"


def test_idle_stream_keeps_health_and_other_request_responsive_and_disconnect_cleans(
    settings,
) -> None:
    app, session = _dev_app_and_session(settings)
    service = app.state.runtime.agent_stream_service
    registry = app.state.application_runtime.active_stream_registry
    blocking = _BlockingStreamingClient(block_cancel=True)
    service.model_client.delegate = blocking

    async def scenario() -> None:
        disconnect = asyncio.Event()
        body = json.dumps(
            _stream_body(session_id=session.id, key="cm-asgi-disconnect")
        ).encode()
        stream_task, stream_started, stream_messages = _launch_asgi(
            app,
            method="POST",
            path="/v1/chat/stream",
            headers={
                **_DEV_AUTH,
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
            },
            body=body,
            disconnect=disconnect,
        )
        await asyncio.wait_for(stream_started.wait(), timeout=2)
        assert await asyncio.to_thread(blocking.session.entered.wait, 2)

        health_task, _health_started, health_messages = _launch_asgi(
            app,
            method="GET",
            path="/health",
        )
        sessions_task, _sessions_started, sessions_messages = _launch_asgi(
            app,
            method="GET",
            path="/v1/sessions",
            headers=_DEV_AUTH,
        )
        await asyncio.wait_for(health_task, timeout=1)
        await asyncio.wait_for(sessions_task, timeout=1)
        assert next(
            message["status"]
            for message in health_messages
            if message["type"] == "http.response.start"
        ) == 200
        assert next(
            message["status"]
            for message in sessions_messages
            if message["type"] == "http.response.start"
        ) == 200

        health_during_cancel_completed = Event()
        cancel_responsiveness: list[bool] = []

        def release_blocked_cancel() -> None:
            if not blocking.session.cancel_entered.wait(timeout=2):
                cancel_responsiveness.append(False)
                blocking.session.cancel_release.set()
                return
            cancel_responsiveness.append(
                health_during_cancel_completed.wait(timeout=0.75)
            )
            blocking.session.cancel_release.set()

        cancel_watchdog = Thread(target=release_blocked_cancel)
        cancel_watchdog.start()
        disconnect.set()
        assert await asyncio.to_thread(blocking.session.cancel_entered.wait, 2)
        cancel_health_task, _cancel_health_started, cancel_health_messages = _launch_asgi(
            app,
            method="GET",
            path="/health",
        )
        await asyncio.wait_for(cancel_health_task, timeout=0.5)
        health_during_cancel_completed.set()
        await asyncio.to_thread(cancel_watchdog.join, 2)
        assert cancel_responsiveness == [True]
        assert next(
            message["status"]
            for message in cancel_health_messages
            if message["type"] == "http.response.start"
        ) == 200
        await asyncio.wait_for(stream_task, timeout=3)
        assert next(
            message["status"]
            for message in stream_messages
            if message["type"] == "http.response.start"
        ) == 200

    try:
        asyncio.run(scenario())
        assert blocking.session.cancelled.is_set()
        assert blocking.session.cancel_reason == "client_disconnected"
        assert blocking.session.close_count == 1
        assert service.model_client.gate.counts()[0] == 0
        assert service.admission.entry_count == 0
        assert registry is not None and registry.active_count == 0
        roles = [
            message.role
            for message in app.state.application_runtime.store.list_messages_by_session(
                session_id=session.id
            )
        ]
        assert roles == ["user"]
    finally:
        app.state.application_runtime.close()


def test_disconnect_intent_wins_before_capacity_one_bridge_abort(settings) -> None:
    for attempt in range(5):
        app, session = _dev_app_and_session(settings)
        service = app.state.runtime.agent_stream_service
        registry = app.state.application_runtime.active_stream_registry
        streaming_client = _BridgePressureStreamingClient()
        trace = _CaptureLogger()
        service.model_client.delegate = streaming_client
        service.turn_engine.trace_logger = trace
        key = f"cm-disconnect-intent-{attempt}"
        message = "bridge pressure"
        handle = service.stream_chat(
            user_context=UserContext(
                user_id=app.state.runtime.settings.dev_auth_user_id,
                request_id=f"req_disconnect_intent_{attempt}",
            ),
            request=StreamChatRequest(
                session_id=session.id,
                character_id="aemeath",
                message=message,
                client_message_id=key,
                metadata={},
            ),
        )

        async def scenario() -> None:
            disconnected = asyncio.Event()

            async def receive():
                await disconnected.wait()
                return {"type": "http.disconnect"}

            request = Request(
                {
                    "type": "http",
                    "method": "POST",
                    "path": "/v1/chat/stream",
                    "headers": [],
                },
                receive=receive,
            )
            request.state.user_id = app.state.runtime.settings.dev_auth_user_id
            response = OwnedStreamingResponse(
                request=request,
                handle=handle,
                app_logger=NoopAppLogger(),
                bridge_capacity=1,
            )
            iterator = response.body_iterator
            assert await anext(iterator) == encode_sse_event(handle.started_event)
            assert await asyncio.to_thread(
                streaming_client.session.second_delta_emitted.wait,
                2,
            )
            disconnected.set()
            try:
                await anext(iterator)
            except StopAsyncIteration:
                pass
            else:
                raise AssertionError("disconnected stream must stop before queued delta")

        try:
            asyncio.run(scenario())
            assert handle._token.reason() == "client_disconnected"
            assert streaming_client.session.cancel_reason == "client_disconnected"
            assert streaming_client.session.cancelled.is_set()
            assert streaming_client.session.close_count == 1
            durable = _resolve_existing(
                app,
                session_id=session.id,
                key=key,
                message=message,
            )
            assert durable.kind == "replay_failed"
            assert durable.record.terminal_reason == "client_disconnected"
            failed_trace = next(
                record
                for record in trace.records
                if record.event_type == "response_stream_failed"
            )
            assert failed_trace.payload["status"] == "client_disconnected"
            assert service.model_client.gate.counts()[0] == 0
            assert service.admission.entry_count == 0
            assert registry is not None and registry.active_count == 0
            roles = [
                stored.role
                for stored in app.state.application_runtime.store.list_messages_by_session(
                    session_id=session.id
                )
            ]
            assert roles == ["user"]
        finally:
            app.state.application_runtime.close()


def test_full_app_asgi_send_oserror_is_suppressed_and_cleans_stream(settings) -> None:
    app, session = _dev_app_and_session(settings)
    service = app.state.runtime.agent_stream_service
    registry = app.state.application_runtime.active_stream_registry
    blocking = _BlockingStreamingClient()
    app_log = _CaptureLogger()
    service.model_client.delegate = blocking
    service.app_logger = app_log
    app.state.runtime = replace(app.state.runtime, app_logger=app_log)
    key = "cm-send-oserror"
    message = "send failure input"
    send_error_marker = "PRIVATE_PROVIDER_BODY_SEND_MARKER"

    async def scenario() -> None:
        body = json.dumps(
            _stream_body(session_id=session.id, key=key, message=message)
        ).encode()
        task, response_started, _messages = _launch_asgi(
            app,
            method="POST",
            path="/v1/chat/stream",
            headers={
                **_DEV_AUTH,
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
            },
            body=body,
            fail_on_body_number=2,
            send_error=OSError(send_error_marker),
        )
        await asyncio.wait_for(response_started.wait(), timeout=2)
        await asyncio.wait_for(task, timeout=3)

    try:
        asyncio.run(scenario())
        assert blocking.session.cancel_reason == "client_disconnected"
        durable = _resolve_existing(
            app,
            session_id=session.id,
            key=key,
            message=message,
        )
        assert durable.kind == "replay_failed"
        assert durable.record.terminal_reason == "client_disconnected"
        assert service.model_client.gate.counts()[0] == 0
        assert service.admission.entry_count == 0
        assert registry is not None and registry.active_count == 0
        assert send_error_marker not in repr(app_log.records)
        roles = [
            stored.role
            for stored in app.state.application_runtime.store.list_messages_by_session(
                session_id=session.id
            )
        ]
        assert roles == ["user"]
    finally:
        app.state.application_runtime.close()


def test_owned_response_client_disconnect_is_suppressed_and_classified(settings) -> None:
    app, session = _dev_app_and_session(settings)
    service = app.state.runtime.agent_stream_service
    registry = app.state.application_runtime.active_stream_registry
    blocking = _BlockingStreamingClient()
    service.model_client.delegate = blocking
    key = "cm-direct-client-disconnect"
    message = "direct response input"
    handle = service.stream_chat(
        user_context=UserContext(
            user_id=app.state.runtime.settings.dev_auth_user_id,
            request_id="req_direct_client_disconnect",
        ),
        request=StreamChatRequest(
            session_id=session.id,
            character_id="aemeath",
            message=message,
            client_message_id=key,
            metadata={},
        ),
    )

    async def scenario() -> None:
        wait_forever = asyncio.Event()

        async def receive():
            await wait_forever.wait()

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": "/v1/chat/stream",
            "raw_path": b"/v1/chat/stream",
            "query_string": b"",
            "root_path": "",
            "headers": [],
            "client": ("127.0.0.1", 51000),
            "server": ("testserver", 443),
        }
        request = Request(scope, receive=receive)
        request.state.user_id = app.state.runtime.settings.dev_auth_user_id
        response = OwnedStreamingResponse(
            request=request,
            handle=handle,
            app_logger=NoopAppLogger(),
        )
        body_send_count = 0

        async def send(message):
            nonlocal body_send_count
            if message["type"] == "http.response.body":
                body_send_count += 1
                if body_send_count == 2:
                    raise ClientDisconnect()

        await response(scope, receive, send)

    try:
        asyncio.run(scenario())
        assert blocking.session.cancel_reason == "client_disconnected"
        durable = _resolve_existing(
            app,
            session_id=session.id,
            key=key,
            message=message,
        )
        assert durable.kind == "replay_failed"
        assert durable.record.terminal_reason == "client_disconnected"
        assert service.model_client.gate.counts()[0] == 0
        assert service.admission.entry_count == 0
        assert registry is not None and registry.active_count == 0
    finally:
        app.state.application_runtime.close()


def test_send_disconnect_does_not_rewrite_durable_completion(settings) -> None:
    app, session = _dev_app_and_session(settings)
    service = app.state.runtime.agent_stream_service
    registry = app.state.application_runtime.active_stream_registry
    service.model_client.delegate = MockStreamingChatModelClient(
        events=(ModelTextDelta("durable answer"), ModelStreamFinished("complete"))
    )
    key = "cm-send-after-commit"
    message = "complete before send failure"

    async def scenario() -> None:
        body = json.dumps(
            _stream_body(session_id=session.id, key=key, message=message)
        ).encode()
        task, response_started, _messages = _launch_asgi(
            app,
            method="POST",
            path="/v1/chat/stream",
            headers={
                **_DEV_AUTH,
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
            },
            body=body,
            fail_on_body_number=3,
            send_error=OSError("PRIVATE_TERMINAL_SEND_MARKER"),
        )
        await asyncio.wait_for(response_started.wait(), timeout=2)
        await asyncio.wait_for(task, timeout=3)

    try:
        asyncio.run(scenario())
        durable = _resolve_existing(
            app,
            session_id=session.id,
            key=key,
            message=message,
        )
        assert durable.kind == "replay_completed"
        assert durable.assistant_message is not None
        assert durable.assistant_message.content == "durable answer"
        assert service.model_client.gate.counts()[0] == 0
        assert service.admission.entry_count == 0
        assert registry is not None and registry.active_count == 0
    finally:
        app.state.application_runtime.close()


def test_sse_encoder_is_compact_utf8_and_omits_domain_only_fields() -> None:
    started = encode_sse_event(
        TurnStarted(
            event_type="turn.started",
            request_id="req_1",
            turn_id="turn_1",
            session_id="session_1",
        )
    )
    delta = encode_sse_event(ResponseDelta(event_type="response.delta", delta="你\n好"))
    completed = encode_sse_event(
        TurnCompleted(
            event_type="turn.completed",
            turn_id="turn_1",
            status="completed",
            warnings=(),
            state_delta={"trust": 0, "affection": 0, "familiarity": 0},
        )
    )
    failed = encode_sse_event(
        TurnFailed(
            event_type="turn.failed",
            turn_id="turn_1",
            error=PublicStreamError(code="MODEL_TIMEOUT", message="Timed out.", retryable=True),
        )
    )

    assert started == (
        b'event: turn.started\ndata: {"request_id":"req_1","turn_id":"turn_1",'
        b'"session_id":"session_1"}\n\n'
    )
    assert delta == 'event: response.delta\ndata: {"delta":"你\\n好"}\n\n'.encode()
    assert b'"event_type"' not in started + delta + completed + failed
    assert b"\nid:" not in started + delta + completed + failed
    assert b"\nretry:" not in started + delta + completed + failed


def test_bridge_backpressure_never_blocks_event_loop() -> None:
    async def scenario() -> None:
        bridge = AsyncStreamBridge(capacity=1)
        bridge.put_initial(
            event=TurnStarted(
                event_type="turn.started",
                request_id="req_bridge",
                turn_id="turn_bridge",
                session_id="session_bridge",
            )
        )
        producer_entered = Event()
        producer_finished = Event()
        errors: list[BaseException] = []

        def produce() -> None:
            producer_entered.set()
            try:
                bridge.put_from_producer(
                    event=ResponseDelta(event_type="response.delta", delta="blocked"),
                    timeout_seconds=0.05,
                )
            except BaseException as exc:
                errors.append(exc)
            finally:
                producer_finished.set()

        thread = Thread(target=produce)
        thread.start()
        assert await asyncio.to_thread(producer_entered.wait, 1)
        loop_probe = asyncio.get_running_loop().create_future()
        asyncio.get_running_loop().call_soon(loop_probe.set_result, None)
        await asyncio.wait_for(loop_probe, timeout=0.2)
        assert await asyncio.to_thread(producer_finished.wait, 1)
        thread.join(timeout=1)
        bridge.abort_from_any_thread()
        assert isinstance(errors[0], StreamConsumerSlowError)

    asyncio.run(scenario())


def test_bridge_abort_wakes_idle_consumer_and_pending_producer() -> None:
    async def scenario() -> None:
        bridge = AsyncStreamBridge(capacity=1)
        bridge.put_initial(
            event=TurnStarted(
                event_type="turn.started",
                request_id="req_abort",
                turn_id="turn_abort",
                session_id="session_abort",
            )
        )
        producer_entered = Event()
        producer_finished = Event()
        errors: list[BaseException] = []

        def produce() -> None:
            producer_entered.set()
            try:
                bridge.put_from_producer(
                    event=ResponseDelta(event_type="response.delta", delta="pending"),
                    timeout_seconds=5,
                )
            except BaseException as exc:
                errors.append(exc)
            finally:
                producer_finished.set()

        thread = Thread(target=produce)
        thread.start()
        assert await asyncio.to_thread(producer_entered.wait, 1)
        bridge.abort_from_any_thread()
        assert await asyncio.to_thread(producer_finished.wait, 1)
        received = await bridge.receive(timeout_seconds=1)
        thread.join(timeout=1)
        assert received.closed is True
        assert isinstance(errors[0], RuntimeError)

    asyncio.run(scenario())


def test_owned_response_heartbeat_is_adapter_only_and_close_is_offloaded() -> None:
    class _IdleHandle:
        request_id = "req_idle"
        turn_id = "turn_idle"
        session_id = "session_idle"
        started_event = TurnStarted(
            event_type="turn.started",
            request_id=request_id,
            turn_id=turn_id,
            session_id=session_id,
        )

        def __init__(self) -> None:
            self.cancel_reason = None
            self.closed = Event()

        def start(self, *, sink) -> None:
            self.sink = sink

        def request_cancel(self, *, reason: str) -> bool:
            if self.cancel_reason is not None:
                return False
            self.cancel_reason = reason
            return True

        def cancel(self, *, reason: str) -> None:
            self.request_cancel(reason=reason)

        def close(self) -> None:
            self.closed.set()

    async def scenario() -> None:
        never_disconnect = asyncio.Event()

        async def receive():
            await never_disconnect.wait()

        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/v1/chat/stream",
                "headers": [],
            },
            receive=receive,
        )
        request.state.user_id = "user_idle"
        handle = _IdleHandle()
        response = OwnedStreamingResponse(
            request=request,
            handle=handle,
            app_logger=NoopAppLogger(),
            heartbeat_seconds=0.01,
        )
        iterator = response.body_iterator
        assert await anext(iterator) == encode_sse_event(handle.started_event)
        assert await anext(iterator) == SSE_HEARTBEAT_FRAME
        await iterator.aclose()
        assert await asyncio.to_thread(handle.closed.wait, 1)
        assert handle.cancel_reason == "sse_generator_closed"

    assert SSE_HEARTBEAT_SECONDS == 15.0
    assert STREAM_BRIDGE_CAPACITY == 32
    asyncio.run(scenario())
