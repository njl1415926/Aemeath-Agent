from __future__ import annotations

from typing import Protocol

from app.schemas import MemoryRecord


class MemoryStore(Protocol):
    def create_memory(
        self,
        *,
        user_id: str,
        namespace: str,
        memory_type: str,
        content: str,
        importance: int,
        confidence: float,
        source: str,
        source_message_id: str | None,
        related_memory_ids: list[str] | None = None,
        expiry: str | None = None,
    ) -> str:
        ...

    def list_active_memories(self, *, user_id: str, limit: int = 50) -> list[MemoryRecord]:
        ...

    def search_memories_keyword(
        self,
        *,
        user_id: str,
        keywords: list[str],
        limit: int = 8,
    ) -> list[MemoryRecord]:
        ...

    def has_exact_active_memory(self, *, user_id: str, content: str) -> bool:
        ...

    def mark_memory_used(self, *, memory_id: str) -> None:
        ...

    def archive_memory(self, *, memory_id: str) -> None:
        ...

    def disable_memory(self, *, memory_id: str) -> None:
        ...

