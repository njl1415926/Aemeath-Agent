from __future__ import annotations

from dataclasses import dataclass

from app.embeddings.artifacts import (
    EmbeddingArtifactValidationError,
    canonical_content_hash,
    validate_stored_embedding_artifact,
)
from app.memory.schemas import MemoryEmbeddingArtifactGapReason


@dataclass(frozen=True)
class StoredMemoryEmbeddingArtifact:
    signature_json: object
    signature_fingerprint: object
    purpose: object
    content_hash: object
    vector_format: object
    vector_dim: object
    vector_bytes: object
    vector_sha256: object


def classify_memory_embedding_artifact_gap(
    *,
    target_content: object,
    expected_signature_json: str,
    artifact: StoredMemoryEmbeddingArtifact | None,
    has_any_canonical_artifact: bool,
    has_legacy_embedding: bool,
) -> MemoryEmbeddingArtifactGapReason | None:
    if artifact is None:
        if not has_any_canonical_artifact and has_legacy_embedding:
            return "legacy_only"
        return "missing"
    if artifact.signature_json != expected_signature_json:
        return "signature_corrupt"
    try:
        validate_stored_embedding_artifact(
            signature_json=artifact.signature_json,
            signature_fingerprint=artifact.signature_fingerprint,
            content_hash=artifact.content_hash,
            purpose=artifact.purpose,
            vector_format=artifact.vector_format,
            vector_dim=artifact.vector_dim,
            vector_bytes=artifact.vector_bytes,
            vector_sha256=artifact.vector_sha256,
        )
    except EmbeddingArtifactValidationError as exc:
        if exc.reason.startswith("signature_"):
            return "signature_corrupt"
        return "vector_corrupt"
    try:
        if canonical_content_hash(target_content) != artifact.content_hash:
            return "content_stale"
    except EmbeddingArtifactValidationError:
        return "content_stale"
    return None
