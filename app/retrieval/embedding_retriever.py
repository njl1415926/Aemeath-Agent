from __future__ import annotations

from typing import Any

from app.retrieval.embeddings import EmbeddingProviderPort
from app.retrieval.schemas import RetrievalResult
from app.retrieval.vector_index import SqliteVectorStore, VectorSearchResult

SOURCE_WEIGHTS = {
    "favor_role_info": 1.20,
    "favor_story": 1.15,
    "favor_word_lore": 1.10,
    "favor_word": 1.10,
    "flow_scene_window": 1.00,
    "flow_voice_context": 1.00,
    "favor_good": 0.90,
}

INTENT_NAMESPACE_WEIGHTS = {
    "query_lore": {
        "canon:profile": 1.20,
        "canon:lore": 1.10,
        "canon:plot": 1.05,
        "canon:voice": 0.70,
    },
    "query_lore_and_voice": {
        "canon:profile": 1.10,
        "canon:lore": 1.05,
        "canon:plot": 1.05,
        "canon:voice": 1.05,
    },
    "query_voice": {
        "canon:voice": 1.15,
        "canon:lore": 0.85,
    },
    "forced_lore": {
        "canon:profile": 1.20,
        "canon:lore": 1.10,
        "canon:plot": 1.05,
        "canon:voice": 0.70,
    },
    "forced_voice": {
        "canon:voice": 1.15,
        "canon:lore": 0.85,
    },
    "forced_lore_and_voice": {
        "canon:profile": 1.10,
        "canon:lore": 1.05,
        "canon:plot": 1.05,
        "canon:voice": 1.05,
    },
}


class SqliteEmbeddingRetriever:
    def __init__(
        self,
        *,
        store: SqliteVectorStore,
        embedding_provider: EmbeddingProviderPort,
    ):
        self.store = store
        self.embedding_provider = embedding_provider

    def retrieve(
        self,
        *,
        query: str,
        user_id: str | None = None,
        namespaces: list[str] | None = None,
        limit: int = 8,
        metadata: dict[str, Any] | None = None,
    ) -> list[RetrievalResult]:
        query_embedding = self.embedding_provider.embed_query(query)
        candidates = self.store.search(
            query_embedding=query_embedding,
            namespaces=namespaces,
            limit=limit,
            candidate_limit=max(limit * 5, limit + 12),
        )
        metadata = metadata or {}
        retrieval_intent = metadata.get("retrieval_intent", "")
        ranked = sorted(
            candidates,
            key=lambda item: self._final_score(item, retrieval_intent=retrieval_intent),
            reverse=True,
        )
        return [
            self._to_result(item, retrieval_intent=retrieval_intent, query_metadata=metadata)
            for item in ranked[:limit]
        ]

    def _final_score(self, item: VectorSearchResult, *, retrieval_intent: str) -> float:
        source_weight = SOURCE_WEIGHTS.get(item.chunk.source_type, 1.0)
        namespace_weight = INTENT_NAMESPACE_WEIGHTS.get(retrieval_intent, {}).get(
            item.chunk.namespace,
            1.0,
        )
        length_weight = self._length_weight(item.chunk.content)
        return item.raw_score * source_weight * namespace_weight * length_weight

    def _length_weight(self, content: str) -> float:
        length = len(content)
        if length < 20:
            return 0.50
        if length < 60:
            return 0.85
        return 1.0

    def _to_result(
        self,
        item: VectorSearchResult,
        *,
        retrieval_intent: str,
        query_metadata: dict[str, Any],
    ) -> RetrievalResult:
        metadata = dict(item.chunk.metadata)
        raw_score = item.raw_score
        final_score = self._final_score(item, retrieval_intent=retrieval_intent)
        metadata.update(
            {
                "retriever": "sqlite_embedding_retriever",
                "raw_score": raw_score,
                "score": final_score,
                "source_weight": SOURCE_WEIGHTS.get(item.chunk.source_type, 1.0),
                "retrieval_intent": retrieval_intent,
                "policy_reason": query_metadata.get("policy_reason", []),
                "lore_score": query_metadata.get("lore_score", 0),
                "voice_score": query_metadata.get("voice_score", 0),
                "no_retrieval_score": query_metadata.get("no_retrieval_score", 0),
                "namespace_weight": INTENT_NAMESPACE_WEIGHTS.get(retrieval_intent, {}).get(
                    item.chunk.namespace,
                    1.0,
                ),
            }
        )
        return RetrievalResult(
            id=item.chunk.id,
            namespace=item.chunk.namespace,
            content=item.chunk.content,
            source_type=item.chunk.source_type,
            confidence=final_score,
            metadata=metadata,
        )
