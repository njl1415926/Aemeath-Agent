from __future__ import annotations

import json
from dataclasses import asdict, replace

import numpy as np
import pytest

from app.embeddings.errors import EmbeddingInvalidInputError
from app.embeddings.providers.hash import DeterministicHashEmbeddingProvider
from app.embeddings.schemas import EmbeddingRequest


def test_hash_batch_first_result_preserves_order_and_vector_contract() -> None:
    provider = DeterministicHashEmbeddingProvider(dimension=8)

    result = provider.embed(
        EmbeddingRequest(texts=("alpha", "beta", "alpha"), purpose="query")
    )

    assert result.vectors.shape == (3, 8)
    assert result.vectors.dtype == np.dtype("<f4")
    assert result.vectors.flags.c_contiguous
    np.testing.assert_allclose(
        np.linalg.norm(result.vectors.astype(np.float64), axis=1),
        np.ones(3),
        atol=1e-6,
    )
    np.testing.assert_array_equal(result.vectors[0], result.vectors[2])
    assert not np.array_equal(result.vectors[0], result.vectors[1])
    np.testing.assert_array_equal(result.vectors[0], provider.embed_query("alpha"))
    np.testing.assert_array_equal(result.vectors[1], provider.embed_query("beta"))
    assert result.signature is provider.signature
    assert result.usage is not None
    assert result.usage.input_count == 3


def test_hash_keeps_legacy_algorithm_and_helper_return_types() -> None:
    provider = DeterministicHashEmbeddingProvider(dimension=8)
    expected = np.zeros(8, dtype=np.float32)
    expected[2] = np.float32(1.0 / np.sqrt(5.0))
    expected[6] = np.float32(-2.0 / np.sqrt(5.0))

    query = provider.embed_query("Alpha beta alpha")
    documents = provider.embed_texts(["Alpha beta alpha", "beta"])

    assert provider.model_name == "deterministic_hash_v1"
    assert provider.dimension == 8
    assert isinstance(query, np.ndarray)
    assert isinstance(documents, list)
    assert all(isinstance(vector, np.ndarray) for vector in documents)
    assert documents[0] is not query
    np.testing.assert_allclose(query, expected, atol=1e-7)
    np.testing.assert_allclose(documents[0], expected, atol=1e-7)


def test_hash_empty_batch_is_valid_but_blank_text_is_rejected() -> None:
    provider = DeterministicHashEmbeddingProvider(dimension=8)

    result = provider.embed(EmbeddingRequest(texts=(), purpose="document"))

    assert result.vectors.shape == (0, 8)
    assert result.vectors.dtype == np.dtype("<f4")
    assert result.vectors.flags.c_contiguous
    assert provider.embed_texts([]) == []

    for blank in ("", " ", "\n\t"):
        with pytest.raises(EmbeddingInvalidInputError) as exc_info:
            provider.embed(EmbeddingRequest(texts=(blank,), purpose="query"))
        assert exc_info.value.reason == "blank_text"


def test_hash_rejects_noncanonical_request_shapes() -> None:
    provider = DeterministicHashEmbeddingProvider(dimension=8)

    with pytest.raises(EmbeddingInvalidInputError) as purpose_error:
        provider.embed(EmbeddingRequest(texts=("alpha",), purpose="search"))  # type: ignore[arg-type]
    assert purpose_error.value.reason == "unsupported_purpose"

    with pytest.raises(EmbeddingInvalidInputError) as texts_error:
        provider.embed(EmbeddingRequest(texts=["alpha"], purpose="query"))  # type: ignore[arg-type]
    assert texts_error.value.reason == "texts_must_be_tuple"


def test_signature_canonical_json_and_fingerprint_are_stable() -> None:
    signature = DeterministicHashEmbeddingProvider().signature
    expected = json.dumps(
        asdict(signature),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )

    assert signature.canonical() == expected
    assert signature.canonical() == signature.canonical()
    assert signature.fingerprint() == signature.fingerprint()
    assert signature.fingerprint().startswith("sha256:")
    assert len(signature.fingerprint()) == len("sha256:") + 64
    assert list(json.loads(signature.canonical())) == sorted(asdict(signature))

    changed = replace(signature, preprocessing_version="legacy-token-regex-v2")
    assert changed.canonical() != signature.canonical()
    assert changed.fingerprint() != signature.fingerprint()
