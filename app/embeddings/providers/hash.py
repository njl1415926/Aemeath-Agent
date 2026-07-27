from __future__ import annotations

import hashlib
import re

import numpy as np

from app.embeddings.errors import EmbeddingConfigurationError
from app.embeddings.providers.base import BaseEmbeddingProvider
from app.embeddings.schemas import (
    EmbeddingBatchResult,
    EmbeddingCallDiagnostics,
    EmbeddingModelSignature,
    EmbeddingRequest,
    EmbeddingSpaceProfile,
    EmbeddingUsage,
)
from app.embeddings.validation import (
    empty_vectors,
    normalize_and_validate_vectors,
    validate_request,
)

TOKEN_RE = re.compile(r"[\w]+|[\u4e00-\u9fff]")


def _direct_hash_signature(dimension: int) -> EmbeddingModelSignature:
    profile_id = "deterministic_hash_v1"
    if dimension != 384:
        profile_id = f"deterministic_hash_v1_dim_{dimension}"
    return EmbeddingModelSignature(
        schema_version="embedding-model-signature/v1",
        provider="deterministic_hash",
        model_id="deterministic_hash_v1",
        dimension=dimension,
        model_revision="sha1-signed-token-bucket-v1",
        pooling="signed_token_bucket",
        normalization="l2",
        query_instruction="",
        document_instruction="",
        preprocessing_version="legacy-token-regex-v1",
        postprocessing_version="normalized-float32-v1",
        profile_id=profile_id,
        canary_set_version="hash_canary_v1",
        expected_fingerprint=None,
    )


class DeterministicHashEmbeddingProvider(BaseEmbeddingProvider):
    """Deterministic test/development provider; never a runtime failure fallback."""

    def __init__(
        self,
        *,
        dimension: int = 384,
        profile: EmbeddingSpaceProfile | None = None,
    ):
        if isinstance(dimension, bool) or not isinstance(dimension, int) or dimension <= 0:
            raise EmbeddingConfigurationError("hash_dimension_invalid")
        signature = profile.signature if profile is not None else _direct_hash_signature(dimension)
        if (
            signature.provider != "deterministic_hash"
            or signature.model_id != "deterministic_hash_v1"
            or signature.dimension != dimension
            or signature.model_revision != "sha1-signed-token-bucket-v1"
            or signature.pooling != "signed_token_bucket"
            or signature.normalization != "l2"
            or signature.query_instruction
            or signature.document_instruction
        ):
            raise EmbeddingConfigurationError("hash_profile_mismatch")
        super().__init__(signature=signature, legacy_model_name="deterministic_hash_v1")

    def embed(self, request: EmbeddingRequest) -> EmbeddingBatchResult:
        validate_request(request)
        if not request.texts:
            vectors = empty_vectors(self.dimension)
        else:
            raw = np.stack([self._embed_legacy(text) for text in request.texts])
            vectors = normalize_and_validate_vectors(
                raw,
                input_count=len(request.texts),
                dimension=self.dimension,
            )
        return EmbeddingBatchResult(
            vectors=vectors,
            signature=self.signature,
            usage=EmbeddingUsage(input_count=len(request.texts), input_tokens=None),
            diagnostics=EmbeddingCallDiagnostics(),
        )

    def _embed_legacy(self, text: str) -> np.ndarray:
        vector = np.zeros(self.dimension, dtype=np.float32)
        tokens = TOKEN_RE.findall(text.lower())
        for token in tokens:
            digest = hashlib.sha1(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "little") % self.dimension
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vector[index] += sign
        norm = float(np.linalg.norm(vector))
        if norm > 0:
            vector /= norm
        return vector
