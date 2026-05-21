from __future__ import annotations

import re

from app.memory.schemas import MemoryQuery, MemorySearchResult
from app.memory.store import MemoryStore
from app.schemas import MemoryRecord


class MemoryReader:
    def __init__(self, store: MemoryStore):
        self.store = store

    def _extract_keywords(self, text: str) -> list[str]:
        chinese_tokens = [
            token
            for token in re.findall(r"[\u4e00-\u9fff]{2,}", text)
            if len(token) <= 10
        ]
        ascii_tokens = [token.lower() for token in re.findall(r"[A-Za-z0-9_]{3,}", text)]
        return list(dict.fromkeys(chinese_tokens + ascii_tokens))

    def retrieve_query(self, *, memory_query: MemoryQuery) -> list[MemorySearchResult]:
        tokens = self._extract_keywords(memory_query.query_text)
        memories = self.store.search_memories_keyword(
            user_id=memory_query.user_id,
            keywords=tokens,
            limit=memory_query.limit,
        )
        recall_keywords = ["记得", "还记得", "回忆", "之前"]
        if (not memories) and any(
            keyword in memory_query.query_text for keyword in recall_keywords
        ):
            memories = self.store.list_active_memories(
                user_id=memory_query.user_id,
                limit=memory_query.limit,
            )
        if memory_query.namespaces:
            memories = [m for m in memories if m.namespace in memory_query.namespaces]
        if memory_query.memory_types:
            memories = [m for m in memories if m.memory_type in memory_query.memory_types]
        if memory_query.min_confidence is not None:
            memories = [m for m in memories if m.confidence >= memory_query.min_confidence]
        for memory in memories:
            self.store.mark_memory_used(memory_id=memory.id)
        return [
            MemorySearchResult(
                record=memory,
                score=float(memory.importance),
                match_reason="keyword_or_recall_fallback",
            )
            for memory in memories
        ]

    def retrieve(self, *, user_id: str, query: str, limit: int = 8) -> list[MemoryRecord]:
        results = self.retrieve_query(
            memory_query=MemoryQuery(
                user_id=user_id,
                query_text=query,
                limit=limit,
            )
        )
        return [result.record for result in results]
