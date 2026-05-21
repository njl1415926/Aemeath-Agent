from __future__ import annotations

from app.observability.schemas import TraceEvent


class NoopTraceLogger:
    def record(self, event: TraceEvent) -> None:
        return None
