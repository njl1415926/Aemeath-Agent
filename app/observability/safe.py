from __future__ import annotations

from threading import Lock

from app.observability.ports import AppLoggerPort, TraceLoggerPort
from app.observability.schemas import AppLogRecord, TraceEvent


class _SafeLoggerState:
    def __init__(self) -> None:
        self._failure_lock = Lock()
        self.failure_count = 0
        self.last_error_type: str | None = None

    def _record_failure(self, exc: Exception) -> None:
        with self._failure_lock:
            self.failure_count += 1
            self.last_error_type = type(exc).__name__


class SafeAppLogger(_SafeLoggerState):
    def __init__(self, delegate: AppLoggerPort):
        super().__init__()
        self.delegate = delegate

    def record(self, record: AppLogRecord) -> None:
        try:
            self.delegate.record(record)
        except Exception as exc:  # observability is best effort
            self._record_failure(exc)


class SafeTraceLogger(_SafeLoggerState):
    def __init__(self, delegate: TraceLoggerPort):
        super().__init__()
        self.delegate = delegate

    def record(self, event: TraceEvent) -> None:
        try:
            self.delegate.record(event)
        except Exception as exc:  # observability is best effort
            self._record_failure(exc)
