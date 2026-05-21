from __future__ import annotations

from typing import Protocol

from app.observability.schemas import TraceEvent


class TraceLoggerPort(Protocol):
    def record(self, event: TraceEvent) -> None:
        ...
