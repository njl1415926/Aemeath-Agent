from __future__ import annotations

from app.observability.schemas import AppLogRecord, PromptRunRecord, TraceEvent


class NoopTraceLogger:
    def record(self, event: TraceEvent) -> None:
        return None


class NoopAppLogger:
    def record(self, record: AppLogRecord) -> None:
        return None


class NoopPromptRunLogger:
    def record(self, record: PromptRunRecord) -> None:
        return None
