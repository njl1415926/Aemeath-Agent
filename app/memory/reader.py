from __future__ import annotations

import re

from app.memory.schemas import MemoryQuery, MemorySearchResult
from app.memory.store import MemoryStore
from app.retrieval.embeddings import EmbeddingProviderPort
from app.schemas import MemoryRecord


class MemoryReader:
    def __init__(
        self,
        store: MemoryStore,
        *,
        embedding_provider: EmbeddingProviderPort | None = None,
        recall_top_k: int = 4,
        recall_threshold: float = 0.45,
    ):
        self.store = store
        self.embedding_provider = embedding_provider
        self.recall_top_k = recall_top_k
        self.recall_threshold = recall_threshold

    def _should_read(self, *, memory_query: MemoryQuery, tokens: list[str]) -> bool:
        if memory_query.reason in {"forced", "memory_focus"}:
            return True
        recall_keywords = [
            "\u8bb0\u5f97",
            "\u8fd8\u8bb0\u5f97",
            "\u56de\u5fc6",
            "\u4e4b\u524d",
        ]
        if any(keyword in memory_query.query_text for keyword in recall_keywords):
            return True
        low_information_inputs = {
            "\u4f60\u597d",
            "\u55e8",
            "hi",
            "hello",
            "\u65e9\u4e0a\u597d",
            "\u665a\u4e0a\u597d",
        }
        normalized = memory_query.query_text.strip().lower()
        if normalized in low_information_inputs:
            return False
        return bool(tokens)

    def _extract_keywords(self, text: str) -> list[str]:
        chinese_tokens = [
            token
            for token in re.findall(r"[\u4e00-\u9fff]{2,}", text)
            if len(token) <= 10
        ]
        ascii_tokens = [token.lower() for token in re.findall(r"[A-Za-z0-9_]{3,}", text)]
        return list(dict.fromkeys(chinese_tokens + ascii_tokens))

    def retrieve_query(self, *, memory_query: MemoryQuery) -> list[MemorySearchResult]:
        if self.embedding_provider is not None:
            return self._retrieve_embedding(memory_query=memory_query)
        return self._retrieve_keyword(memory_query=memory_query)

    def _retrieve_keyword(self, *, memory_query: MemoryQuery) -> list[MemorySearchResult]:
        tokens = self._extract_keywords(memory_query.query_text)
        if not self._should_read(memory_query=memory_query, tokens=tokens):
            return []
        memories = self.store.search_memories_keyword(
            user_id=memory_query.user_id,
            character_id=memory_query.character_id,
            session_id=memory_query.session_id,
            keywords=tokens,
            limit=memory_query.limit,
        )
        recall_keywords = [
            "\u8bb0\u5f97",
            "\u8fd8\u8bb0\u5f97",
            "\u56de\u5fc6",
            "\u4e4b\u524d",
        ]
        if (not memories) and any(
            keyword in memory_query.query_text for keyword in recall_keywords
        ):
            memories = self.store.list_recallable_memories(
                user_id=memory_query.user_id,
                character_id=memory_query.character_id,
                session_id=memory_query.session_id,
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

    def _retrieve_embedding(self, *, memory_query: MemoryQuery) -> list[MemorySearchResult]:
        tokens = self._extract_keywords(memory_query.query_text)
        if not self._should_read(memory_query=memory_query, tokens=tokens):
            return []
        query_embedding = self.embedding_provider.embed_query(memory_query.query_text)
        vector_results = self.store.search_memory_embeddings(
            user_id=memory_query.user_id,
            character_id=memory_query.character_id,
            session_id=memory_query.session_id,
            query_embedding=query_embedding,
            limit=max(memory_query.limit, self.recall_top_k),
            candidate_limit=max(self.recall_top_k * 4, self.recall_top_k + 8),
        )
        output: list[MemorySearchResult] = []
        for item in vector_results:
            if item.score < self.recall_threshold:
                continue
            memory = self.store.get_memory(memory_id=item.memory_id)
            if memory is None or memory.status != "active" or not memory.is_active:
                continue
            if memory_query.namespaces and memory.namespace not in memory_query.namespaces:
                continue
            if memory_query.memory_types and memory.memory_type not in memory_query.memory_types:
                continue
            if (
                memory_query.min_confidence is not None
                and memory.confidence < memory_query.min_confidence
            ):
                continue
            self.store.mark_memory_used(memory_id=memory.id)
            output.append(
                MemorySearchResult(
                    record=memory,
                    score=item.score,
                    match_reason="embedding_similarity",
                )
            )
            if len(output) >= self.recall_top_k:
                break
        return output

    def retrieve(self, *, user_id: str, query: str, limit: int = 8) -> list[MemoryRecord]:
        results = self.retrieve_query(
            memory_query=MemoryQuery(
                user_id=user_id,
                query_text=query,
                limit=limit,
            )
        )
        return [result.record for result in results]
