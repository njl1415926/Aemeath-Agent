from __future__ import annotations

import re

from app.embeddings.artifacts import prepare_embedding_query
from app.embeddings.errors import EmbeddingInvalidResponseError
from app.embeddings.ports import EmbeddingProviderPort, QueryEmbeddingSourcePort
from app.embeddings.query import DirectQueryEmbeddingSource
from app.errors import map_exception
from app.memory.observability import record_memory_event
from app.memory.schemas import (
    MemoryArtifactEligibility,
    MemoryQuery,
    MemorySearchResult,
)
from app.memory.store import MemoryStore
from app.observability.noop import NoopAppLogger
from app.observability.ports import AppLoggerPort
from app.schemas import MemoryRecord

_EXPLICIT_RECALL_KEYWORDS = ("记得", "还记得", "回忆", "之前")


class MemoryReader:
    def __init__(
        self,
        store: MemoryStore,
        *,
        query_embedding_source: QueryEmbeddingSourcePort | None = None,
        embedding_provider: EmbeddingProviderPort | None = None,
        recall_top_k: int = 4,
        recall_threshold: float = 0.45,
        app_logger: AppLoggerPort | None = None,
    ):
        if query_embedding_source is not None and embedding_provider is not None:
            raise ValueError("configure query_embedding_source or embedding_provider, not both")
        self.store = store
        self.query_embedding_source = query_embedding_source or (
            DirectQueryEmbeddingSource(embedding_provider)
            if embedding_provider is not None
            else None
        )
        self.recall_top_k = recall_top_k
        self.recall_threshold = recall_threshold
        self.app_logger = app_logger or NoopAppLogger()

    def _is_explicit_recall(self, memory_query: MemoryQuery) -> bool:
        return memory_query.reason in {"forced", "memory_focus"} or any(
            keyword in memory_query.query_text for keyword in _EXPLICIT_RECALL_KEYWORDS
        )

    def _should_read(self, *, memory_query: MemoryQuery, tokens: list[str]) -> bool:
        if self._is_explicit_recall(memory_query):
            return True
        low_information_inputs = {
            "你好",
            "嗨",
            "hi",
            "hello",
            "早上好",
            "晚上好",
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
        tokens = self._extract_keywords(memory_query.query_text)
        if not self._should_read(memory_query=memory_query, tokens=tokens):
            return []
        if self.query_embedding_source is None:
            return self._finalize(
                self._keyword_results(memory_query=memory_query, tokens=tokens)
            )
        return self._retrieve_embedding(memory_query=memory_query, tokens=tokens)

    def _keyword_results(
        self,
        *,
        memory_query: MemoryQuery,
        tokens: list[str],
    ) -> list[MemorySearchResult]:
        fetch_limit = max(memory_query.limit * 4, 50)
        memories = (
            self.store.search_memories_keyword(
                user_id=memory_query.user_id,
                character_id=memory_query.character_id,
                session_id=memory_query.session_id,
                keywords=tokens,
                limit=fetch_limit,
            )
            if tokens
            else []
        )
        if not memories and self._is_explicit_recall(memory_query):
            memories = self.store.list_recallable_memories(
                user_id=memory_query.user_id,
                character_id=memory_query.character_id,
                session_id=memory_query.session_id,
                limit=fetch_limit,
            )
        memories = self._filter_records(memories, memory_query=memory_query)
        return [
            MemorySearchResult(
                record=memory,
                score=float(memory.importance),
                match_reason="keyword_or_recall_fallback",
            )
            for memory in memories[: memory_query.limit]
        ]

    def _retrieve_embedding(
        self,
        *,
        memory_query: MemoryQuery,
        tokens: list[str],
    ) -> list[MemorySearchResult]:
        assert self.query_embedding_source is not None
        try:
            resolved = self.query_embedding_source.resolve(
                query_text=memory_query.query_text
            )
            if (
                resolved.signature.canonical()
                != self.query_embedding_source.signature.canonical()
            ):
                raise EmbeddingInvalidResponseError("query_signature_mismatch")
            prepared_query = prepare_embedding_query(
                signature=resolved.signature,
                vector=resolved.vector,
            )
        except Exception as exc:  # Query generation must not take down the dialogue turn.
            fallback = self._keyword_results(memory_query=memory_query, tokens=tokens)
            error_info = map_exception(exc)
            record_memory_event(
                self.app_logger,
                event_name="memory.recall_degraded",
                level="warning",
                error_code=error_info.code.value,
                payload={
                    "fallback_strategy": "keyword",
                    "keyword_result_count": len(fallback),
                },
                user_id=memory_query.user_id,
                session_id=memory_query.session_id,
                character_id=memory_query.character_id,
            )
            return self._finalize(fallback)

        outcome = self.store.search_memory_embedding_artifacts(
            eligibility=MemoryArtifactEligibility(
                user_id=memory_query.user_id,
                character_id=memory_query.character_id,
                session_id=memory_query.session_id,
                namespaces=tuple(memory_query.namespaces),
                memory_types=tuple(memory_query.memory_types),
                min_confidence=memory_query.min_confidence,
            ),
            query_signature=resolved.signature,
            query_embedding=prepared_query.vector,
            limit=max(memory_query.limit, self.recall_top_k),
            candidate_limit=max(self.recall_top_k * 4, self.recall_top_k + 8),
        )
        result_limit = max(1, min(memory_query.limit, self.recall_top_k))
        vector_results: list[MemorySearchResult] = []
        for item in outcome.hits:
            if item.score < self.recall_threshold:
                continue
            memory = self.store.get_memory(memory_id=item.memory_id)
            if memory is None:
                continue
            eligible_records = self._filter_records(
                [memory],
                memory_query=memory_query,
            )
            if not eligible_records:
                continue
            vector_results.append(
                MemorySearchResult(
                    record=eligible_records[0],
                    score=item.score,
                    match_reason="embedding_similarity",
                )
            )
            if len(vector_results) >= result_limit:
                break

        compatible_count = outcome.diagnostics.compatible_count
        coverage_complete = compatible_count == outcome.eligible_memory_count
        fallback_strategy = "none"
        keyword_results: list[MemorySearchResult] = []
        if not vector_results:
            fallback_strategy = "keyword"
            keyword_results = self._keyword_results(
                memory_query=memory_query,
                tokens=tokens,
            )
        elif not coverage_complete and len(vector_results) < result_limit:
            fallback_strategy = "keyword_supplement"
            keyword_results = self._keyword_results(
                memory_query=memory_query,
                tokens=tokens,
            )

        merged = list(vector_results)
        seen_ids = {result.record.id for result in merged}
        for result in keyword_results:
            if result.record.id in seen_ids:
                continue
            merged.append(result)
            seen_ids.add(result.record.id)
            if len(merged) >= result_limit:
                break

        record_memory_event(
            self.app_logger,
            event_name="memory.recall_completed",
            level="info" if coverage_complete else "warning",
            payload={
                "provider": resolved.signature.provider,
                "profile_id": resolved.signature.profile_id,
                "eligible_memory_count": outcome.eligible_memory_count,
                "compatible_memory_count": compatible_count,
                "coverage_complete": coverage_complete,
                "vector_result_count": len(vector_results),
                "keyword_result_count": max(0, len(merged) - len(vector_results)),
                "fallback_strategy": fallback_strategy,
            },
            user_id=memory_query.user_id,
            session_id=memory_query.session_id,
            character_id=memory_query.character_id,
        )
        return self._finalize(merged[:result_limit])

    def _filter_records(
        self,
        memories: list[MemoryRecord],
        *,
        memory_query: MemoryQuery,
    ) -> list[MemoryRecord]:
        output = [
            memory
            for memory in memories
            if memory.status == "active" and memory.is_active
        ]
        if memory_query.namespaces:
            output = [m for m in output if m.namespace in memory_query.namespaces]
        if memory_query.memory_types:
            output = [m for m in output if m.memory_type in memory_query.memory_types]
        if memory_query.min_confidence is not None:
            output = [m for m in output if m.confidence >= memory_query.min_confidence]
        return output

    def _finalize(
        self,
        results: list[MemorySearchResult],
    ) -> list[MemorySearchResult]:
        seen_ids: set[str] = set()
        output: list[MemorySearchResult] = []
        for result in results:
            if result.record.id in seen_ids:
                continue
            seen_ids.add(result.record.id)
            self.store.mark_memory_used(memory_id=result.record.id)
            output.append(result)
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
