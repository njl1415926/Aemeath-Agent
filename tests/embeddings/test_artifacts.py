from __future__ import annotations

import hashlib

import numpy as np
import pytest

from app.embeddings.artifacts import (
    EmbeddingArtifactValidationError,
    EmbeddingArtifactWrite,
    canonical_content_hash,
    canonicalize_embedding_text,
    embedding_content_hash,
    prepare_embedding_artifact,
)
from app.embeddings.providers.hash import DeterministicHashEmbeddingProvider


def test_artifact_text_canonicalization_normalizes_nfc_and_newlines_without_stripping():
    canonical = canonicalize_embedding_text("  cafe\u0301\r\ntrailing space \r")

    assert canonical == "  caf\u00e9\ntrailing space \n"
    assert canonical_content_hash("cafe\u0301") == embedding_content_hash("caf\u00e9")
    assert canonical_content_hash(" value ") != canonical_content_hash("value")


def test_artifact_signature_and_vector_digest_are_canonical_f32le_facts():
    signature = DeterministicHashEmbeddingProvider(dimension=3).signature
    vector = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    prepared = prepare_embedding_artifact(
        EmbeddingArtifactWrite(
            signature=signature,
            content_hash=canonical_content_hash("digest source"),
            vector=vector,
        )
    )

    assert prepared.signature_json == signature.canonical()
    assert prepared.signature_fingerprint == signature.fingerprint()
    assert prepared.vector_bytes == vector.astype("<f4").tobytes()
    assert prepared.vector_sha256 == "sha256:" + hashlib.sha256(
        prepared.vector_bytes
    ).hexdigest()


@pytest.mark.parametrize(
    ("vector", "reason"),
    [
        (np.array([np.nan, 0.0, 0.0], dtype=np.float32), "vector_non_finite"),
        (np.array([np.inf, 0.0, 0.0], dtype=np.float32), "vector_non_finite"),
        (np.zeros(3, dtype=np.float32), "vector_zero_norm"),
    ],
)
def test_artifact_write_rejects_invalid_vectors(vector, reason):
    signature = DeterministicHashEmbeddingProvider(dimension=3).signature

    with pytest.raises(EmbeddingArtifactValidationError) as raised:
        prepare_embedding_artifact(
            EmbeddingArtifactWrite(
                signature=signature,
                content_hash=canonical_content_hash("invalid vector"),
                vector=vector,
            )
        )

    assert raised.value.reason == reason
