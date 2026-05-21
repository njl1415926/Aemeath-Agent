from __future__ import annotations

from app.memory.store import MemoryStore
from app.schemas import MemoryDecision


class MemoryWriter:
    def __init__(self, store: MemoryStore):
        self.store = store

    def persist_decision(
        self,
        *,
        user_id: str,
        decision: MemoryDecision,
        source_message_id: str | None = None,
    ) -> str | None:
        if not decision.should_write:
            return None
        if decision.operation not in {"create", "update", "merge"}:
            return None
        if not decision.content or not decision.namespace or not decision.memory_type:
            return None

        if self.store.has_exact_active_memory(user_id=user_id, content=decision.content):
            return None

        memory_id = self.store.create_memory(
            user_id=user_id,
            namespace=decision.namespace,
            memory_type=decision.memory_type,
            content=decision.content,
            importance=decision.importance or 5,
            confidence=decision.confidence or 0.8,
            source=decision.source or "inferred_from_dialogue",
            source_message_id=source_message_id,
            related_memory_ids=decision.related_memory_ids,
            expiry=decision.expiry,
        )
        return memory_id
