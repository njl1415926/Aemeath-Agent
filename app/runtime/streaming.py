from __future__ import annotations

from threading import Condition, Lock, get_ident
from typing import Literal, Protocol

from app.errors import redact_sensitive
from app.observability.noop import NoopAppLogger
from app.observability.ports import AppLoggerPort
from app.observability.schemas import AppLogRecord
from app.runtime.guardrails.errors import RuntimeBusyError


class RuntimeOwnedStreamHandle(Protocol):
    handle_id: str
    turn_id: str
    owns_in_progress_turn: bool

    def cancel(self, *, reason: str) -> None: ...

    def close(self) -> None: ...


class ActiveStreamRegistry:
    """Own active stream handles and close them before model transports."""

    def __init__(self, *, app_logger: AppLoggerPort | None = None) -> None:
        self._condition = Condition(Lock())
        self._state: Literal["open", "closing", "closed"] = "open"
        self._closing_thread_id: int | None = None
        self._handles: dict[str, RuntimeOwnedStreamHandle] = {}
        self._app_logger = app_logger or NoopAppLogger()

    def register(self, handle: RuntimeOwnedStreamHandle) -> None:
        with self._condition:
            if self._state != "open":
                raise RuntimeBusyError("stream_registry_closing")
            if handle.handle_id in self._handles:
                raise RuntimeError("stream_handle_already_registered")
            self._handles[handle.handle_id] = handle

    def deregister(self, *, handle_id: str) -> None:
        with self._condition:
            self._handles.pop(handle_id, None)
            self._condition.notify_all()

    def is_live_turn(self, *, turn_id: str) -> bool:
        with self._condition:
            return any(
                handle.owns_in_progress_turn and handle.turn_id == turn_id
                for handle in self._handles.values()
            )

    @property
    def active_count(self) -> int:
        with self._condition:
            return len(self._handles)

    @property
    def is_open(self) -> bool:
        with self._condition:
            return self._state == "open"

    def close(self) -> None:
        current_thread_id = get_ident()
        with self._condition:
            if self._state == "closed":
                return
            if self._state == "closing":
                if self._closing_thread_id == current_thread_id:
                    return
                while self._state != "closed":
                    self._condition.wait()
                return
            self._state = "closing"
            self._closing_thread_id = current_thread_id
            handles = tuple(self._handles.values())

        self._record(
            event_name="runtime.stream_registry_closing",
            level="info",
            payload={"active_count": len(handles)},
        )
        for handle in handles:
            try:
                handle.cancel(reason="runtime_closing")
            except BaseException as exc:
                self._record_close_failure(handle=handle, error=exc)
        for handle in handles:
            try:
                handle.close()
            except BaseException as exc:
                self._record_close_failure(handle=handle, error=exc)

        with self._condition:
            for handle in handles:
                if self._handles.get(handle.handle_id) is handle:
                    del self._handles[handle.handle_id]
            self._state = "closed"
            self._closing_thread_id = None
            self._condition.notify_all()

    def _record_close_failure(
        self,
        *,
        handle: RuntimeOwnedStreamHandle,
        error: BaseException,
    ) -> None:
        self._record(
            event_name="runtime.stream_close_failed",
            level="error",
            payload={
                "turn_id": handle.turn_id,
                "error_type": type(error).__name__,
            },
        )

    def _record(self, *, event_name: str, level: str, payload: dict) -> None:
        try:
            self._app_logger.record(
                AppLogRecord(
                    event_name=event_name,
                    level=level,
                    payload=redact_sensitive(payload),
                )
            )
        except Exception:
            return
