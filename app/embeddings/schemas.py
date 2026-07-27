from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

import numpy as np

EmbeddingPurpose = Literal["query", "document"]
EmbeddingProviderId = Literal["siliconflow", "local_bge_m3", "deterministic_hash"]
ProfileReleaseStatus = Literal["review", "approved"]


@dataclass(frozen=True)
class EmbeddingModelSignature:
    schema_version: str
    provider: str
    model_id: str
    dimension: int
    model_revision: str
    pooling: str
    normalization: str
    query_instruction: str
    document_instruction: str
    preprocessing_version: str
    postprocessing_version: str
    profile_id: str
    canary_set_version: str
    expected_fingerprint: str | None

    def canonical(self) -> str:
        return json.dumps(
            asdict(self),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def fingerprint(self) -> str:
        digest = hashlib.sha256(self.canonical().encode("utf-8")).hexdigest()
        return f"sha256:{digest}"


@dataclass(frozen=True)
class EmbeddingRequest:
    texts: tuple[str, ...]
    purpose: EmbeddingPurpose


@dataclass(frozen=True)
class EmbeddingUsage:
    input_count: int
    input_tokens: int | None = None


@dataclass(frozen=True)
class EmbeddingCallDiagnostics:
    """Safe per-call diagnostics.

    ``provider_request_id`` is populated only for a single provider chunk.  The
    ordered tuple retains one safe id (or ``None``) per chunk for split calls.
    """

    retry_count: int = 0
    provider_request_id: str | None = None
    provider_request_ids: tuple[str | None, ...] = ()


@dataclass(frozen=True)
class EmbeddingBatchResult:
    vectors: np.ndarray
    signature: EmbeddingModelSignature
    usage: EmbeddingUsage | None = None
    diagnostics: EmbeddingCallDiagnostics | None = None


@dataclass(frozen=True)
class ResolvedQueryEmbedding:
    signature: EmbeddingModelSignature
    vector: np.ndarray


@dataclass(frozen=True)
class QualificationThresholds:
    min_cosine_per_vector: float
    max_abs_coordinate_delta: float


@dataclass(frozen=True)
class EmbeddingSpaceProfile:
    schema_version: str
    release_status: ProfileReleaseStatus
    profile_id: str
    provider: EmbeddingProviderId
    model_id: str
    dimension: int
    model_revision: str
    pooling: str
    normalization: str
    query_instruction: str
    document_instruction: str
    preprocessing_version: str
    postprocessing_version: str
    canary_set_version: str
    canary_artifact: str
    canary_artifact_sha256: str
    baseline_artifact: str | None
    baseline_artifact_sha256: str | None
    fingerprint_algorithm: str
    expected_fingerprint: str | None
    qualification_thresholds: QualificationThresholds | None

    @property
    def signature(self) -> EmbeddingModelSignature:
        return EmbeddingModelSignature(
            schema_version="embedding-model-signature/v1",
            provider=self.provider,
            model_id=self.model_id,
            dimension=self.dimension,
            model_revision=self.model_revision,
            pooling=self.pooling,
            normalization=self.normalization,
            query_instruction=self.query_instruction,
            document_instruction=self.document_instruction,
            preprocessing_version=self.preprocessing_version,
            postprocessing_version=self.postprocessing_version,
            profile_id=self.profile_id,
            canary_set_version=self.canary_set_version,
            expected_fingerprint=self.expected_fingerprint,
        )


@dataclass(frozen=True)
class EmbeddingRuntimeOptions:
    """Runtime-only knobs that do not identify an embedding vector space.

    ``batch_size`` is the maximum provider-internal chunk size, not a limit on
    the number of texts accepted by the batch-first Port.
    """

    batch_size: int = 16
    timeout_seconds: float = 30.0
    max_retries: int = 2
    retry_backoff_seconds: float = 0.25
    local_model_path: Path = Path(".cache/huggingface/BAAI/bge-m3")
    local_device: str = "auto"
    local_max_length: int = 8192
    lazy_load: bool = True
    allow_hash: bool = False


@dataclass(frozen=True)
class CanaryItem:
    canary_id: str
    text: str


@dataclass(frozen=True)
class CanarySet:
    schema_version: str
    canary_set_version: str
    items: tuple[CanaryItem, ...]


@dataclass(frozen=True)
class QualificationVectorMetrics:
    canary_id: str
    cosine: float
    max_abs_coordinate_delta: float


@dataclass(frozen=True)
class QualificationResult:
    passed: bool
    expected_fingerprint: str
    observed_fingerprint: str
    metrics: tuple[QualificationVectorMetrics, ...]
