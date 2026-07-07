from __future__ import annotations

from typing import Protocol

from app.observability.schemas import AppLogRecord, PromptRunRecord, TraceEvent


class TraceLoggerPort(Protocol):
    def record(self, event: TraceEvent) -> None:
        ...


class AppLoggerPort(Protocol):
    def record(self, record: AppLogRecord) -> None:
        ...


class PromptRunLoggerPort(Protocol):
    def record(self, record: PromptRunRecord) -> None:
        ...
