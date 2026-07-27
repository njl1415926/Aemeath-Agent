from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from functools import cache
from typing import Literal

import numpy as np

from app.embeddings.schemas import EmbeddingModelSignature

EmbeddingArtifactPurpose = Literal["document"]
EmbeddingVectorFormat = Literal["f32le-v1"]

EMBEDDING_CONTENT_HASH_DOMAIN = b"aemeath-embedding-content:v1\x00"
EMBEDDING_VECTOR_FORMAT_F32LE_V1: EmbeddingVectorFormat = "f32le-v1"
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_VECTOR_NORM_TOLERANCE = 1e-5


class EmbeddingArtifactValidationError(ValueError):
    """Raised when persisted embedding artifact facts fail the shared contract."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class EmbeddingArtifactWrite:
    """A caller-produced document vector ready for transactional persistence.

    The caller derives ``content_hash`` from the exact canonical text supplied to
    the provider. The store independently verifies it against the current target
    content before writing, so a concurrent content change cannot bind a stale
    vector to a newer target.
    """

    signature: EmbeddingModelSignature
    content_hash: str
    vector: np.ndarray
    purpose: EmbeddingArtifactPurpose = "document"


@dataclass(frozen=True)
class PreparedEmbeddingArtifact:
    signature_json: str
    signature_fingerprint: str
    content_hash: str
    purpose: EmbeddingArtifactPurpose
    vector_format: EmbeddingVectorFormat
    vector_dim: int
    vector_bytes: bytes
    vector_sha256: str


@dataclass(frozen=True)
class PreparedEmbeddingQuery:
    signature_json: str
    signature_fingerprint: str
    vector: np.ndarray


@dataclass(frozen=True)
class EmbeddingArtifactDiagnostic:
    reason: str
    count: int = 1


@dataclass(frozen=True)
class EmbeddingArtifactDiagnostics:
    scanned_count: int = 0
    compatible_count: int = 0
    issues: tuple[EmbeddingArtifactDiagnostic, ...] = ()


@dataclass(frozen=True)
class EmbeddingArtifactUpsertOutcome:
    status: Literal["created", "reused", "replaced"]
    diagnostics: tuple[EmbeddingArtifactDiagnostic, ...] = ()


def canonicalize_embedding_text(text: str) -> str:
    if not isinstance(text, str):
        raise EmbeddingArtifactValidationError("content_not_string")
    normalized = unicodedata.normalize("NFC", text)
    return normalized.replace("\r\n", "\n").replace("\r", "\n")


def embedding_content_hash(canonical_text: str) -> str:
    if canonical_text != canonicalize_embedding_text(canonical_text):
        raise EmbeddingArtifactValidationError("content_not_canonical")
    digest = hashlib.sha256(
        EMBEDDING_CONTENT_HASH_DOMAIN + canonical_text.encode("utf-8")
    ).hexdigest()
    return f"sha256:{digest}"


def canonical_content_hash(text: str) -> str:
    return embedding_content_hash(canonicalize_embedding_text(text))


def prepare_embedding_artifact(write: EmbeddingArtifactWrite) -> PreparedEmbeddingArtifact:
    if write.purpose != "document":
        raise EmbeddingArtifactValidationError("unsupported_artifact_purpose")
    _validate_sha256(write.content_hash, "content_hash_invalid")
    signature_json = write.signature.canonical()
    fingerprint = write.signature.fingerprint()
    vector = _validated_vector(write.vector, dimension=write.signature.dimension)
    vector_bytes = vector.tobytes()
    return PreparedEmbeddingArtifact(
        signature_json=signature_json,
        signature_fingerprint=fingerprint,
        content_hash=write.content_hash,
        purpose=write.purpose,
        vector_format=EMBEDDING_VECTOR_FORMAT_F32LE_V1,
        vector_dim=write.signature.dimension,
        vector_bytes=vector_bytes,
        vector_sha256=_sha256(vector_bytes),
    )


def prepare_embedding_query(
    *,
    signature: EmbeddingModelSignature,
    vector: np.ndarray,
) -> PreparedEmbeddingQuery:
    return PreparedEmbeddingQuery(
        signature_json=signature.canonical(),
        signature_fingerprint=signature.fingerprint(),
        vector=_validated_vector(vector, dimension=signature.dimension),
    )


def validate_stored_embedding_artifact(
    *,
    signature_json: str,
    signature_fingerprint: str,
    content_hash: str,
    purpose: str,
    vector_format: str,
    vector_dim: int,
    vector_bytes: bytes,
    vector_sha256: str,
) -> tuple[EmbeddingModelSignature, np.ndarray]:
    signature = deserialize_signature(signature_json, signature_fingerprint)
    _validate_sha256(content_hash, "content_hash_invalid")
    if purpose != "document":
        raise EmbeddingArtifactValidationError("artifact_purpose_invalid")
    if vector_format != EMBEDDING_VECTOR_FORMAT_F32LE_V1:
        raise EmbeddingArtifactValidationError("vector_format_invalid")
    if vector_dim != signature.dimension:
        raise EmbeddingArtifactValidationError("vector_dim_signature_mismatch")
    if not isinstance(vector_bytes, bytes):
        raise EmbeddingArtifactValidationError("vector_blob_not_bytes")
    if len(vector_bytes) != vector_dim * np.dtype("<f4").itemsize:
        raise EmbeddingArtifactValidationError("vector_blob_length_mismatch")
    _validate_sha256(vector_sha256, "vector_sha256_invalid")
    if _sha256(vector_bytes) != vector_sha256:
        raise EmbeddingArtifactValidationError("vector_sha256_mismatch")
    vector = np.frombuffer(vector_bytes, dtype="<f4")
    return signature, _validated_vector(vector, dimension=vector_dim)


@cache
def deterministic_hash_v1_legacy_signature() -> EmbeddingModelSignature:
    """Return the fixed legacy Hash space eligible for verified promotion."""
    # Imported lazily to keep basic artifact DTO use independent of profile I/O.
    from app.embeddings.config import load_embedding_space_profile

    return load_embedding_space_profile("deterministic_hash_v1").signature


def is_promoted_deterministic_hash_legacy(
    *,
    embedding_model: object,
    vector_dim: object,
    legacy_vector_bytes: object,
    target_content: object,
    artifact_signature_json: object,
    artifact_signature_fingerprint: object,
    artifact_purpose: object,
    artifact_content_hash: object,
    artifact_vector_format: object,
    artifact_vector_dim: object,
    artifact_vector_bytes: object,
    artifact_vector_sha256: object,
) -> bool:
    """Whether a legacy row is the verified Hash artifact already promoted.

    This is deliberately independent of the active query space. A target can
    hold a usable Hash artifact and an unrelated BGE legacy row at the same
    time; only the legacy Hash row that exactly matches the canonical artifact
    is no longer reported as unverifiable.
    """
    if embedding_model != "deterministic_hash_v1" or vector_dim != 384:
        return False
    expected_signature = deterministic_hash_v1_legacy_signature()
    if (
        artifact_signature_json != expected_signature.canonical()
        or artifact_signature_fingerprint != expected_signature.fingerprint()
    ):
        return False
    try:
        if canonical_content_hash(target_content) != artifact_content_hash:
            return False
        validate_stored_embedding_artifact(
            signature_json=artifact_signature_json,
            signature_fingerprint=artifact_signature_fingerprint,
            content_hash=artifact_content_hash,
            purpose=artifact_purpose,
            vector_format=artifact_vector_format,
            vector_dim=artifact_vector_dim,
            vector_bytes=artifact_vector_bytes,
            vector_sha256=artifact_vector_sha256,
        )
        return bytes(legacy_vector_bytes) == artifact_vector_bytes
    except (EmbeddingArtifactValidationError, TypeError, ValueError, OverflowError):
        return False


def deserialize_signature(
    signature_json: str,
    signature_fingerprint: str,
) -> EmbeddingModelSignature:
    if not isinstance(signature_json, str):
        raise EmbeddingArtifactValidationError("signature_json_invalid")
    _validate_sha256(signature_fingerprint, "signature_fingerprint_invalid")
    try:
        value = json.loads(signature_json)
        if not isinstance(value, dict):
            raise TypeError("signature_not_object")
        signature = EmbeddingModelSignature(**value)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise EmbeddingArtifactValidationError("signature_json_invalid") from exc
    if signature.canonical() != signature_json:
        raise EmbeddingArtifactValidationError("signature_json_not_canonical")
    if signature.fingerprint() != signature_fingerprint:
        raise EmbeddingArtifactValidationError("signature_fingerprint_mismatch")
    return signature


def diagnostics_from_reasons(reasons: list[str]) -> tuple[EmbeddingArtifactDiagnostic, ...]:
    counts: dict[str, int] = {}
    for reason in reasons:
        counts[reason] = counts.get(reason, 0) + 1
    return tuple(
        EmbeddingArtifactDiagnostic(reason=reason, count=count)
        for reason, count in sorted(counts.items())
    )


def _validated_vector(vector: object, *, dimension: int) -> np.ndarray:
    if isinstance(dimension, bool) or not isinstance(dimension, int) or dimension <= 0:
        raise EmbeddingArtifactValidationError("vector_dim_invalid")
    try:
        array = np.asarray(vector, dtype="<f4")
    except (TypeError, ValueError, OverflowError) as exc:
        raise EmbeddingArtifactValidationError("vector_not_numeric") from exc
    if array.ndim != 1:
        raise EmbeddingArtifactValidationError("vector_rank_invalid")
    if array.shape[0] != dimension:
        raise EmbeddingArtifactValidationError("vector_dim_mismatch")
    if not np.all(np.isfinite(array)):
        raise EmbeddingArtifactValidationError("vector_non_finite")
    norm = float(np.linalg.norm(array.astype(np.float64)))
    if not np.isfinite(norm) or norm <= 0.0:
        raise EmbeddingArtifactValidationError("vector_zero_norm")
    if abs(norm - 1.0) > _VECTOR_NORM_TOLERANCE:
        raise EmbeddingArtifactValidationError("vector_not_normalized")
    return np.ascontiguousarray(array, dtype="<f4")


def _validate_sha256(value: str, reason: str) -> None:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise EmbeddingArtifactValidationError(reason)


def _sha256(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"
