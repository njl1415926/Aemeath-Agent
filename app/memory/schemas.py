from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from app.embeddings.artifacts import EmbeddingArtifactDiagnostics
from app.schemas import MemoryRecord

MemoryNamespace = Literal[
    "user_profile",
    "interaction:user_profile",
    "interaction:preference",
    "interaction:project",
    "interaction:relationship",
]
MemoryScope = Literal["user_global", "character", "session"]


@dataclass(frozen=True)
class MemoryQuery:
    user_id: str
    query_text: str
    character_id: str | None = None
    session_id: str | None = None
    namespaces: list[str] = field(default_factory=list)
    memory_types: list[str] = field(default_factory=list)
    limit: int = 8
    min_confidence: float | None = None
    reason: str = "dialogue_context"


@dataclass(frozen=True)
class MemorySearchResult:
    record: MemoryRecord
    score: float
    match_reason: str


MemoryExtractionJobStatus = Literal["pending", "running", "completed", "failed"]
MemoryExtractionTriggerType = Literal["hard", "window", "manual"]


@dataclass(frozen=True)
class MemoryExtractionJobRecord:
    id: str
    idempotency_key: str
    user_id: str
    session_id: str
    character_id: str
    trigger_type: str
    status: str
    source_message_ids: list[str]
    input_payload: dict[str, Any]
    result_payload: dict[str, Any] | None
    error: str | None
    attempt_count: int
    locked_by: str | None
    locked_at: str | None
    created_at: str
    updated_at: str | None


@dataclass(frozen=True)
class MemoryJobEnqueueResult:
    job: MemoryExtractionJobRecord
    created: bool


@dataclass(frozen=True)
class ExtractedMemory:
    content: str
    memory_type: str
    importance: int
    confidence: float
    source_message_ids: list[str]
    reason: str


@dataclass(frozen=True)
class MemoryExtractionResult:
    created_memory_ids: list[str]
    existing_memory_ids: list[str]
    artifact_written_memory_ids: list[str]
    embedding_pending_memory_ids: list[str]
    extracted_memories: list[ExtractedMemory]


@dataclass(frozen=True)
class MemoryVectorSearchResult:
    memory_id: str
    score: float


@dataclass(frozen=True)
class ExactActiveMemoryResult:
    memory_id: str
    created: bool


@dataclass(frozen=True)
class MemoryArtifactEligibility:
    user_id: str
    character_id: str | None
    session_id: str | None
    namespaces: tuple[str, ...] = ()
    memory_types: tuple[str, ...] = ()
    min_confidence: float | None = None


MemoryEmbeddingArtifactGapReason = Literal[
    "missing",
    "content_stale",
    "vector_corrupt",
    "signature_corrupt",
    "legacy_only",
]


@dataclass(frozen=True)
class MemoryEmbeddingArtifactGapCursor:
    created_at: str
    memory_id: str


@dataclass(frozen=True)
class MemoryEmbeddingArtifactGap:
    memory_id: str
    user_id: str
    content: str
    reason: MemoryEmbeddingArtifactGapReason
    created_at: str


@dataclass(frozen=True)
class MemoryEmbeddingArtifactGapPage:
    gaps: list[MemoryEmbeddingArtifactGap]
    next_cursor: MemoryEmbeddingArtifactGapCursor | None
    scanned_memory_count: int


@dataclass(frozen=True)
class MemoryEmbeddingSearchOutcome:
    hits: list[MemoryVectorSearchResult]
    diagnostics: EmbeddingArtifactDiagnostics
    eligible_memory_count: int


@dataclass(frozen=True)
class MemoryEmbeddingRepairResult:
    scanned_memory_count: int
    gap_count: int
    artifact_written_memory_ids: list[str]
    embedding_pending_memory_ids: list[str]
    next_cursor: MemoryEmbeddingArtifactGapCursor | None
    error_code: str | None = None
