from __future__ import annotations

import asyncio
import json
from concurrent.futures import (
    CancelledError as FutureCancelledError,
)
from concurrent.futures import (
    Future,
)
from concurrent.futures import (
    TimeoutError as FutureTimeoutError,
)
from dataclasses import dataclass
from threading import Lock, get_ident
from typing import Any, Coroutine

from fastapi import Request
from fastapi.responses import StreamingResponse
from starlette.requests import ClientDisconnect
from starlette.types import ASGIApp, Receive, Scope, Send

from app.errors import redact_sensitive
from app.observability.ports import AppLoggerPort
from app.observability.schemas import AppLogRecord
from app.services.stream_schemas import (
    ChatStreamEvent,
    ChatStreamHandle,
    ResponseDelta,
    StreamConsumerSlowError,
    StreamEventSink,
    TurnCompleted,
    TurnFailed,
    TurnStarted,
)

SSE_HEARTBEAT_SECONDS = 15.0
STREAM_BRIDGE_CAPACITY = 32
SSE_HEARTBEAT_FRAME = b": heartbeat\n\n"

_BRIDGE_PRODUCER_CLOSED = object()
_BRIDGE_ABORTED = object()
_SSE_RESPONSE_OWNER_STATE_KEY = "_aemeath_sse_response_owner"


class _SSEDownstreamDisconnect(Exception):
    pass


class SSEClientDisconnectMiddleware:
    """Suppress post-header SSE send disconnects and notify the response owner."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") != "/v1/chat/stream":
            await self._app(scope, receive, send)
            return
        response_started = False

        async def guarded_send(message):
            nonlocal response_started
            try:
                await send(message)
            except (OSError, ClientDisconnect) as exc:
                if not response_started:
                    raise
                owner = scope.get("state", {}).get(_SSE_RESPONSE_OWNER_STATE_KEY)
                if isinstance(owner, OwnedStreamingResponse):
                    await owner.disconnect_from_downstream()
                raise _SSEDownstreamDisconnect() from exc
            if message["type"] == "http.response.start":
                response_started = True

        try:
            await self._app(scope, receive, guarded_send)
        except _SSEDownstreamDisconnect:
            return


@dataclass(frozen=True)
class BridgeReceiveResult:
    event: ChatStreamEvent | None = None
    timed_out: bool = False
    closed: bool = False


class AsyncStreamBridge(StreamEventSink):
    """Bounded thread-to-event-loop bridge for provider-neutral stream events."""

    def __init__(self, *, capacity: int = STREAM_BRIDGE_CAPACITY) -> None:
        if capacity <= 0:
            raise ValueError("stream_bridge_capacity_invalid")
        self._loop = asyncio.get_running_loop()
        self._loop_thread_id = get_ident()
        self._queue: asyncio.Queue[ChatStreamEvent | object] = asyncio.Queue(
            maxsize=capacity
        )
        self._lock = Lock()
        self._aborted = False
        self._producer_closed = False
        self._pending: set[Future[Any]] = set()

    def put_initial(self, *, event: TurnStarted) -> None:
        if get_ident() != self._loop_thread_id:
            raise RuntimeError("stream_bridge_initial_put_wrong_thread")
        with self._lock:
            if self._aborted:
                raise RuntimeError("stream_bridge_closed")
        self._queue.put_nowait(event)

    def put_from_producer(
        self,
        *,
        event: ChatStreamEvent,
        timeout_seconds: float,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("stream_bridge_put_timeout_invalid")
        if get_ident() == self._loop_thread_id:
            raise RuntimeError("stream_bridge_producer_put_on_event_loop")
        future = self._submit(self._put_event(event))
        try:
            future.result(timeout=timeout_seconds)
        except FutureTimeoutError as exc:
            future.cancel()
            raise StreamConsumerSlowError("stream_bridge_consumer_slow") from exc
        except FutureCancelledError as exc:
            raise RuntimeError("stream_bridge_closed") from exc
        finally:
            self._discard_pending(future)

    def close_from_producer(self) -> None:
        with self._lock:
            if self._aborted or self._producer_closed:
                return
            self._producer_closed = True
        try:
            future = self._submit(self._put_control(_BRIDGE_PRODUCER_CLOSED))
        except RuntimeError:
            return
        future.add_done_callback(self._consume_control_future)

    async def receive(self, *, timeout_seconds: float) -> BridgeReceiveResult:
        if timeout_seconds <= 0:
            raise ValueError("stream_bridge_receive_timeout_invalid")
        try:
            item = await asyncio.wait_for(
                self._queue.get(),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            return BridgeReceiveResult(timed_out=True)
        if item is _BRIDGE_PRODUCER_CLOSED or item is _BRIDGE_ABORTED:
            return BridgeReceiveResult(closed=True)
        return BridgeReceiveResult(event=item)  # type: ignore[arg-type]

    def abort_from_any_thread(self) -> None:
        with self._lock:
            if self._aborted:
                return
            self._aborted = True
            pending = tuple(self._pending)
        for future in pending:
            future.cancel()
        try:
            self._loop.call_soon_threadsafe(self._abort_on_loop)
        except RuntimeError:
            return

    async def _put_event(self, event: ChatStreamEvent) -> None:
        with self._lock:
            if self._aborted:
                raise RuntimeError("stream_bridge_closed")
        await self._queue.put(event)

    async def _put_control(self, item: object) -> None:
        with self._lock:
            if self._aborted:
                return
        await self._queue.put(item)

    def _submit(self, coroutine: Coroutine[Any, Any, None]) -> Future[Any]:
        with self._lock:
            if self._aborted:
                coroutine.close()
                raise RuntimeError("stream_bridge_closed")
        try:
            future = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        except BaseException:
            coroutine.close()
            raise
        with self._lock:
            if self._aborted:
                future.cancel()
            self._pending.add(future)
        return future

    def _consume_control_future(self, future: Future[Any]) -> None:
        self._discard_pending(future)
        if future.cancelled():
            return
        try:
            future.exception()
        except BaseException:
            return

    def _discard_pending(self, future: Future[Any]) -> None:
        with self._lock:
            self._pending.discard(future)

    def _abort_on_loop(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._queue.put_nowait(_BRIDGE_ABORTED)


class OwnedStreamingResponse(StreamingResponse):
    """Own bridge and handle cleanup for one HTTP SSE response."""

    def __init__(
        self,
        *,
        request: Request,
        handle: ChatStreamHandle,
        app_logger: AppLoggerPort,
        heartbeat_seconds: float = SSE_HEARTBEAT_SECONDS,
        bridge_capacity: int = STREAM_BRIDGE_CAPACITY,
    ) -> None:
        if heartbeat_seconds <= 0:
            raise ValueError("sse_heartbeat_interval_invalid")
        self._request = request
        self._handle = handle
        self._app_logger = app_logger
        self._heartbeat_seconds = heartbeat_seconds
        self._bridge_capacity = bridge_capacity
        self._bridge: AsyncStreamBridge | None = None
        self._terminal_delivered = False
        self._disconnect_observed = False
        self._disconnect_logged = False
        request.scope.setdefault("state", {})[_SSE_RESPONSE_OWNER_STATE_KEY] = self
        super().__init__(
            content=self._iterate_sse(),
            status_code=200,
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store"},
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def observed_receive():
            message = await receive()
            if message["type"] == "http.disconnect":
                self._observe_client_disconnect()
            return message

        async def observed_send(message):
            try:
                await send(message)
            except (OSError, ClientDisconnect):
                self._observe_client_disconnect()
                raise

        try:
            await super().__call__(scope, observed_receive, observed_send)
        except (OSError, ClientDisconnect):
            self._observe_client_disconnect()
        finally:
            reason = None
            if not self._terminal_delivered:
                reason = (
                    "client_disconnected"
                    if self._disconnect_observed
                    else "sse_generator_closed"
                )
                if reason == "client_disconnected":
                    self._observe_client_disconnect()
                else:
                    self._request_cancellation_intent(reason=reason)
            bridge = self._bridge
            if bridge is not None:
                bridge.abort_from_any_thread()
            await self._cleanup_handle(reason=reason)
            state = scope.get("state", {})
            if state.get(_SSE_RESPONSE_OWNER_STATE_KEY) is self:
                state.pop(_SSE_RESPONSE_OWNER_STATE_KEY, None)

    async def disconnect_from_downstream(self) -> None:
        """Apply disconnect intent and cleanup before an outer send error is suppressed."""
        self._observe_client_disconnect()
        bridge = self._bridge
        if bridge is not None:
            bridge.abort_from_any_thread()
        await self._cleanup_handle(reason="client_disconnected")

    async def _iterate_sse(self):
        bridge: AsyncStreamBridge | None = None
        terminal_delivered = False
        disconnected = False
        cancelled: asyncio.CancelledError | None = None
        try:
            bridge = AsyncStreamBridge(capacity=self._bridge_capacity)
            self._bridge = bridge
            bridge.put_initial(event=self._handle.started_event)
            try:
                await asyncio.to_thread(self._handle.start, sink=bridge)
            except Exception as exc:
                self._record(
                    event_name="api.chat_stream_start_failed",
                    level="error",
                    payload={"error_type": type(exc).__name__},
                )
                await self._cleanup_handle(reason="sse_generator_closed")
            while True:
                if await self._request.is_disconnected():
                    disconnected = True
                    break
                received = await bridge.receive(timeout_seconds=self._heartbeat_seconds)
                if received.timed_out:
                    if await self._request.is_disconnected():
                        disconnected = True
                        break
                    yield SSE_HEARTBEAT_FRAME
                    continue
                if received.closed:
                    break
                event = received.event
                if event is None:
                    raise RuntimeError("stream_bridge_event_missing")
                yield encode_sse_event(event)
                if isinstance(event, (TurnCompleted, TurnFailed)):
                    terminal_delivered = True
                    self._terminal_delivered = True
                    self._record(
                        event_name="api.chat_stream_terminal_delivered",
                        level="info",
                        payload={"event_type": event.event_type, "status": "delivered"},
                    )
                    break
        except asyncio.CancelledError as exc:
            disconnected = True
            cancelled = exc
        except GeneratorExit:
            pass
        except (OSError, ClientDisconnect):
            disconnected = True
        except Exception as exc:
            self._record(
                event_name="api.chat_stream_delivery_failed",
                level="error",
                payload={"error_type": type(exc).__name__},
            )
        finally:
            reason = None
            if not terminal_delivered:
                is_disconnected = disconnected or self._disconnect_observed
                reason = (
                    "client_disconnected" if is_disconnected else "sse_generator_closed"
                )
                if is_disconnected:
                    self._observe_client_disconnect()
                else:
                    self._request_cancellation_intent(reason=reason)
            if bridge is not None:
                bridge.abort_from_any_thread()
            await self._cleanup_handle(reason=reason)
            self._bridge = None
        if cancelled is not None:
            raise cancelled

    async def _cleanup_handle(self, *, reason: str | None) -> None:
        try:
            await asyncio.to_thread(self._cancel_and_close, reason)
        except Exception as exc:
            self._record(
                event_name="api.chat_stream_close_failed",
                level="error",
                payload={"error_type": type(exc).__name__},
            )

    def _request_cancellation_intent(self, *, reason: str) -> None:
        try:
            self._handle.request_cancel(reason=reason)
        except Exception as exc:
            self._record(
                event_name="api.chat_stream_cancel_intent_failed",
                level="error",
                payload={"error_type": type(exc).__name__},
            )

    def _observe_client_disconnect(self) -> None:
        self._disconnect_observed = True
        self._request_cancellation_intent(reason="client_disconnected")
        if self._disconnect_logged:
            return
        self._disconnect_logged = True
        self._record(
            event_name="api.chat_stream_disconnected",
            level="info",
            payload={"status": "client_disconnected"},
        )

    def _cancel_and_close(self, reason: str | None) -> None:
        first_error: BaseException | None = None
        if reason is not None:
            try:
                self._handle.cancel(reason=reason)
            except BaseException as exc:
                first_error = exc
        try:
            self._handle.close()
        except BaseException as exc:
            first_error = first_error or exc
        if first_error is not None:
            raise first_error

    def _record(self, *, event_name: str, level: str, payload: dict[str, Any]) -> None:
        try:
            self._app_logger.record(
                AppLogRecord(
                    event_name=event_name,
                    level=level,
                    request_id=self._handle.request_id,
                    turn_id=self._handle.turn_id,
                    session_id=self._handle.session_id,
                    user_id=getattr(self._request.state, "user_id", None),
                    payload=redact_sensitive(payload),
                )
            )
        except Exception:
            return


def encode_sse_event(event: ChatStreamEvent) -> bytes:
    if isinstance(event, TurnStarted):
        payload = {
            "request_id": event.request_id,
            "turn_id": event.turn_id,
            "session_id": event.session_id,
        }
    elif isinstance(event, ResponseDelta):
        payload = {"delta": event.delta}
    elif isinstance(event, TurnCompleted):
        payload = {
            "turn_id": event.turn_id,
            "status": event.status,
            "warnings": list(event.warnings),
            "state_delta": dict(event.state_delta),
        }
    elif isinstance(event, TurnFailed):
        payload = {
            "turn_id": event.turn_id,
            "error": {
                "code": event.error.code,
                "message": event.error.message,
                "retryable": event.error.retryable,
            },
        }
    else:
        raise TypeError("unsupported_chat_stream_event")
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event.event_type}\ndata: {data}\n\n".encode("utf-8")
