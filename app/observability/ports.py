from __future__ import annotations

from typing import Protocol

from app.observability.schemas import PromptRunRecord, TraceEvent


class TraceLoggerPort(Protocol):
    def record(self, event: TraceEvent) -> None:
        ...


class PromptRunLoggerPort(Protocol):
    def record(self, record: PromptRunRecord) -> None:
        ...
