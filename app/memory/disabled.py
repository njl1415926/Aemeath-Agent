from __future__ import annotations

from app.memory.schemas import MemoryQuery, MemorySearchResult
from app.schemas import MemoryDecision, MemoryRecord


class DisabledMemoryReader:
    def retrieve_query(self, *, memory_query: MemoryQuery) -> list[MemorySearchResult]:
        return []

    def retrieve(self, *, user_id: str, query: str, limit: int = 8) -> list[MemoryRecord]:
        return []


class DisabledMemoryWriter:
    def persist_decision(
        self,
        *,
        user_id: str,
        decision: MemoryDecision,
        source_message_id: str | None = None,
    ) -> str | None:
        return None


class DisabledMemoryWriteClassifier:
    def decide(self, *, user_input: str, assistant_response: str) -> MemoryDecision:
        return MemoryDecision(
            should_write=False,
            operation="skip",
            namespace=None,
            memory_type=None,
            content=None,
            importance=None,
            confidence=None,
            source=None,
            reason="Long-term memory is disabled for the v0.2 minimal runtime.",
        )
