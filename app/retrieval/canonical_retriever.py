from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from app.embeddings.artifacts import EmbeddingArtifactDiagnostics
from app.embeddings.errors import EmbeddingError
from app.embeddings.ports import QueryEmbeddingSourcePort
from app.embeddings.schemas import ResolvedQueryEmbedding
from app.observability.noop import NoopAppLogger
from app.observability.ports import AppLoggerPort
from app.observability.schemas import AppLogRecord
from app.retrieval.errors import RagGenerationStoreError, RagIndexError
from app.retrieval.generation import (
    RagIndexConfig,
    ReadyGenerationHandle,
    ReadyGenerationRegistry,
)
from app.retrieval.index_manifest import sanitize_source_id
from app.retrieval.schemas import RetrievalResult

_SOURCE_WEIGHTS = {
    "favor_role_info": 1.20,
    "favor_story": 1.15,
    "favor_word_lore": 1.10,
    "favor_word": 1.10,
    "flow_scene_window": 1.00,
    "flow_voice_context": 1.00,
    "favor_good": 0.90,
}

_INTENT_NAMESPACE_WEIGHTS = {
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


@dataclass(frozen=True)
class CanonicalRagSearchOutcome:
    hits: list[RetrievalResult]
    generation_id: str | None
    diagnostics: EmbeddingArtifactDiagnostics | None
    availability_reason: str


class CanonicalRagRetriever:
    """RetrieverPort adapter over an exact-signature managed generation."""

    def __init__(
        self,
        *,
        config: RagIndexConfig,
        query_source: QueryEmbeddingSourcePort,
        registry: ReadyGenerationRegistry | None = None,
        app_logger: AppLoggerPort | None = None,
    ) -> None:
        self.config = config
        self.query_source = query_source
        self.registry = registry or ReadyGenerationRegistry(config)
        self.app_logger = app_logger or NoopAppLogger()

    def retrieve(
        self,
        *,
        query: str,
        user_id: str | None = None,
        namespaces: list[str] | None = None,
        limit: int = 8,
        metadata: dict[str, Any] | None = None,
    ) -> list[RetrievalResult]:
        del user_id
        return self.search_with_outcome(
            query=query,
            namespaces=namespaces,
            limit=limit,
            metadata=metadata,
        ).hits

    def search_with_outcome(
        self,
        *,
        query: str,
        namespaces: list[str] | None = None,
        limit: int = 8,
        metadata: dict[str, Any] | None = None,
    ) -> CanonicalRagSearchOutcome:
        self._validate_request(limit=limit, namespaces=namespaces)
        resolution = self.registry.resolve(self.query_source.signature)
        if resolution.handle is None:
            outcome = CanonicalRagSearchOutcome(
                hits=[],
                generation_id=None,
                diagnostics=None,
                availability_reason=resolution.availability_reason,
            )
            self._record_outcome(outcome)
            return outcome
        handle = resolution.handle
        try:
            resolved_query = self.query_source.resolve(query_text=query)
        except EmbeddingError:
            outcome = CanonicalRagSearchOutcome(
                hits=[],
                generation_id=handle.manifest.generation_id,
                diagnostics=None,
                availability_reason="query_embedding_failed",
            )
            self._record_outcome(outcome)
            return outcome
        outcome = self._search_handle(
            handle=handle,
            resolved_query=resolved_query,
            namespaces=namespaces,
            limit=limit,
            metadata=metadata,
        )
        self._record_outcome(outcome)
        return outcome

    def search_resolved(
        self,
        *,
        resolved_query: ResolvedQueryEmbedding,
        namespaces: list[str] | None = None,
        limit: int = 8,
        metadata: dict[str, Any] | None = None,
    ) -> CanonicalRagSearchOutcome:
        """Future TurnSemanticContext entry point that does not call a Provider."""

        self._validate_request(limit=limit, namespaces=namespaces)
        resolution = self.registry.resolve(resolved_query.signature)
        if resolution.handle is None:
            outcome = CanonicalRagSearchOutcome(
                hits=[],
                generation_id=None,
                diagnostics=None,
                availability_reason=resolution.availability_reason,
            )
            self._record_outcome(outcome)
            return outcome
        outcome = self._search_handle(
            handle=resolution.handle,
            resolved_query=resolved_query,
            namespaces=namespaces,
            limit=limit,
            metadata=metadata,
        )
        self._record_outcome(outcome)
        return outcome

    def _search_handle(
        self,
        *,
        handle: ReadyGenerationHandle,
        resolved_query: ResolvedQueryEmbedding,
        namespaces: list[str] | None,
        limit: int,
        metadata: dict[str, Any] | None,
    ) -> CanonicalRagSearchOutcome:
        manifest = handle.manifest
        if (
            resolved_query.signature.canonical() != manifest.embedding_signature_json
            or resolved_query.signature.fingerprint()
            != manifest.embedding_signature_fingerprint
        ):
            return CanonicalRagSearchOutcome(
                hits=[],
                generation_id=manifest.generation_id,
                diagnostics=None,
                availability_reason="signature_mismatch",
            )
        retrieval_intent = str((metadata or {}).get("retrieval_intent", ""))
        try:
            candidates = handle.store.search_embedding_artifacts(
                query_signature=resolved_query.signature,
                query_embedding=resolved_query.vector,
                namespaces=namespaces,
                limit=limit,
                candidate_limit=max(limit * 5, limit + 12),
            )
        except (
            EmbeddingError,
            RagGenerationStoreError,
            RagIndexError,
            OverflowError,
            TypeError,
            ValueError,
            sqlite3.Error,
        ):
            return CanonicalRagSearchOutcome(
                hits=[],
                generation_id=manifest.generation_id,
                diagnostics=None,
                availability_reason="index_corrupt",
            )
        if candidates.diagnostics.issues:
            return CanonicalRagSearchOutcome(
                hits=[],
                generation_id=manifest.generation_id,
                diagnostics=candidates.diagnostics,
                availability_reason="index_corrupt",
            )
        ranked = sorted(
            candidates.hits,
            key=lambda item: self._score(
                raw_score=item.raw_score,
                source_type=item.chunk.source_type,
                namespace=item.chunk.namespace,
                content=item.chunk.content,
                retrieval_intent=retrieval_intent,
            ),
            reverse=True,
        )
        results = [
            RetrievalResult(
                id=item.chunk.id,
                namespace=item.chunk.namespace,
                content=item.chunk.content,
                source_type=item.chunk.source_type,
                confidence=self._score(
                    raw_score=item.raw_score,
                    source_type=item.chunk.source_type,
                    namespace=item.chunk.namespace,
                    content=item.chunk.content,
                    retrieval_intent=retrieval_intent,
                ),
                metadata={
                    "generation_id": manifest.generation_id,
                    "retriever": "canonical_sqlite_embedding_retriever",
                    "raw_score": item.raw_score,
                    "namespace": item.chunk.namespace,
                    "source_type": item.chunk.source_type,
                    "signature_fingerprint": manifest.embedding_signature_fingerprint,
                    "source_id": self._safe_source_id(
                        item.chunk.metadata.get("source_id"),
                        item.chunk.id,
                    ),
                },
            )
            for item in ranked[:limit]
        ]
        return CanonicalRagSearchOutcome(
            hits=results,
            generation_id=manifest.generation_id,
            diagnostics=candidates.diagnostics,
            availability_reason="available",
        )

    def _score(
        self,
        *,
        raw_score: float,
        source_type: str,
        namespace: str,
        content: str,
        retrieval_intent: str,
    ) -> float:
        source_weight = _SOURCE_WEIGHTS.get(source_type, 1.0)
        namespace_weight = _INTENT_NAMESPACE_WEIGHTS.get(retrieval_intent, {}).get(
            namespace,
            1.0,
        )
        if len(content) < 20:
            length_weight = 0.50
        elif len(content) < 60:
            length_weight = 0.85
        else:
            length_weight = 1.0
        return raw_score * source_weight * namespace_weight * length_weight

    def _safe_source_id(self, value: object, fallback: str) -> str:
        return sanitize_source_id(value, fallback=fallback)

    def _validate_request(self, *, limit: int, namespaces: list[str] | None) -> None:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("rag_retrieval_limit_invalid")
        if namespaces is not None and (
            not isinstance(namespaces, list)
            or any(not isinstance(namespace, str) or not namespace for namespace in namespaces)
        ):
            raise ValueError("rag_retrieval_namespaces_invalid")

    def _record_outcome(self, outcome: CanonicalRagSearchOutcome) -> None:
        issue_count = (
            sum(issue.count for issue in outcome.diagnostics.issues)
            if outcome.diagnostics is not None
            else 0
        )
        try:
            self.app_logger.record(
                AppLogRecord(
                    event_name="rag.query_completed",
                    level=(
                        "info" if outcome.availability_reason == "available" else "warning"
                    ),
                    payload={
                        "index_id": self.config.index_id,
                        "generation_id": outcome.generation_id,
                        "availability_reason": outcome.availability_reason,
                        "result_count": len(outcome.hits),
                        "diagnostic_issue_count": issue_count,
                    },
                )
            )
        except Exception:
            return
