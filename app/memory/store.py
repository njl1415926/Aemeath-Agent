from __future__ import annotations

from typing import Any, Protocol

import numpy as np

from app.embeddings.artifacts import EmbeddingArtifactUpsertOutcome, EmbeddingArtifactWrite
from app.embeddings.schemas import EmbeddingModelSignature
from app.memory.schemas import (
    ExactActiveMemoryResult,
    MemoryArtifactEligibility,
    MemoryEmbeddingArtifactGapCursor,
    MemoryEmbeddingArtifactGapPage,
    MemoryEmbeddingSearchOutcome,
    MemoryExtractionJobRecord,
    MemoryJobEnqueueResult,
    MemoryVectorSearchResult,
)
from app.schemas import MemoryRecord, MessageRecord


class MemoryStore(Protocol):
    def create_memory(
        self,
        *,
        user_id: str,
        scope: str,
        scope_character_id: str | None,
        scope_session_id: str | None,
        namespace: str,
        memory_type: str,
        content: str,
        importance: int,
        confidence: float,
        source: str,
        source_message_id: str | None,
        source_session_id: str | None,
        source_job_id: str | None,
        source_message_ids: list[str] | None = None,
        related_memory_ids: list[str] | None = None,
        expiry: str | None = None,
    ) -> str:
        ...

    def create_or_get_exact_active_memory(
        self,
        *,
        user_id: str,
        scope: str,
        scope_character_id: str | None,
        scope_session_id: str | None,
        namespace: str,
        memory_type: str,
        content: str,
        importance: int,
        confidence: float,
        source: str,
        source_message_id: str | None,
        source_session_id: str | None,
        source_job_id: str | None,
        source_message_ids: list[str] | None = None,
        related_memory_ids: list[str] | None = None,
        expiry: str | None = None,
    ) -> ExactActiveMemoryResult:
        ...

    def list_active_memories(self, *, user_id: str, limit: int = 50) -> list[MemoryRecord]:
        ...

    def get_memory(self, *, memory_id: str) -> MemoryRecord | None:
        ...

    def search_memories_keyword(
        self,
        *,
        user_id: str,
        character_id: str | None,
        session_id: str | None,
        keywords: list[str],
        limit: int = 8,
    ) -> list[MemoryRecord]:
        ...

    def list_recallable_memories(
        self,
        *,
        user_id: str,
        character_id: str | None,
        session_id: str | None,
        limit: int = 50,
    ) -> list[MemoryRecord]:
        ...

    def has_exact_active_memory(
        self,
        *,
        user_id: str,
        scope: str,
        scope_character_id: str | None,
        scope_session_id: str | None,
        content: str,
    ) -> bool:
        ...

    def mark_memory_used(self, *, memory_id: str) -> None:
        ...

    def archive_memory(self, *, memory_id: str, user_id: str) -> bool:
        ...

    def disable_memory(self, *, memory_id: str, user_id: str) -> bool:
        ...

    def get_message(self, *, message_id: str) -> MessageRecord | None:
        ...

    def get_recent_messages(self, *, session_id: str, limit: int) -> list[MessageRecord]:
        ...

    def list_unprocessed_window_messages(
        self,
        *,
        session_id: str,
        limit: int,
    ) -> list[MessageRecord]:
        ...

    def enqueue_memory_extraction_job(
        self,
        *,
        idempotency_key: str,
        user_id: str,
        session_id: str,
        character_id: str,
        trigger_type: str,
        source_message_ids: list[str],
        input_payload: dict[str, Any],
    ) -> MemoryJobEnqueueResult:
        ...

    def claim_next_memory_extraction_job(
        self,
        *,
        worker_id: str,
    ) -> MemoryExtractionJobRecord | None:
        ...

    def complete_memory_extraction_job(
        self,
        *,
        job_id: str,
        result_payload: dict[str, Any],
    ) -> None:
        ...

    def fail_memory_extraction_job(self, *, job_id: str, error: str) -> None:
        ...

    def list_memory_extraction_jobs(
        self,
        *,
        session_id: str | None = None,
    ) -> list[MemoryExtractionJobRecord]:
        ...

    def upsert_memory_embedding(
        self,
        *,
        memory_id: str,
        user_id: str,
        embedding_model: str,
        embedding: np.ndarray,
    ) -> None:
        ...

    def search_memory_embeddings(
        self,
        *,
        user_id: str,
        character_id: str | None,
        session_id: str | None,
        query_embedding: np.ndarray,
        limit: int,
        candidate_limit: int | None = None,
    ) -> list[MemoryVectorSearchResult]:
        ...

    def upsert_memory_embedding_artifact(
        self,
        *,
        memory_id: str,
        user_id: str,
        artifact: EmbeddingArtifactWrite,
    ) -> EmbeddingArtifactUpsertOutcome:
        ...

    def search_memory_embedding_artifacts(
        self,
        *,
        eligibility: MemoryArtifactEligibility,
        query_signature: EmbeddingModelSignature,
        query_embedding: np.ndarray,
        limit: int,
        candidate_limit: int | None = None,
    ) -> MemoryEmbeddingSearchOutcome:
        ...

    def list_memory_embedding_artifact_gaps(
        self,
        *,
        query_signature: EmbeddingModelSignature,
        user_id: str | None = None,
        cursor: MemoryEmbeddingArtifactGapCursor | None = None,
        limit: int = 50,
    ) -> MemoryEmbeddingArtifactGapPage:
        ...
