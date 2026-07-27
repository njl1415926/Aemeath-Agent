from __future__ import annotations

import numpy as np

from app.embeddings.providers.hash import (
    DeterministicHashEmbeddingProvider as FoundationHashEmbeddingProvider,
)
from app.retrieval.embeddings import (
    BgeM3EmbeddingProvider,
    DeterministicHashEmbeddingProvider,
    EmbeddingProviderPort,
    cosine_similarity,
)


def test_retrieval_shim_reexports_hash_and_preserves_legacy_return_types() -> None:
    provider: EmbeddingProviderPort = DeterministicHashEmbeddingProvider(dimension=16)

    documents = provider.embed_texts(["first document", "second document"])
    query = provider.embed_query("first document")

    assert DeterministicHashEmbeddingProvider is FoundationHashEmbeddingProvider
    assert provider.model_name == "deterministic_hash_v1"
    assert provider.dimension == 16
    assert isinstance(documents, list)
    assert len(documents) == 2
    assert all(isinstance(vector, np.ndarray) for vector in documents)
    assert all(vector.shape == (16,) for vector in documents)
    assert isinstance(query, np.ndarray)
    assert query.shape == (16,)
    assert np.isclose(cosine_similarity(query, documents[0]), 1.0)


def test_retrieval_shim_local_bge_constructor_remains_lazy(tmp_path) -> None:
    provider: EmbeddingProviderPort = BgeM3EmbeddingProvider(model_path=tmp_path / "model")

    assert provider.model_name == "BAAI/bge-m3"
    assert provider.dimension == 1024
    assert provider.model_path == tmp_path / "model"
    assert provider._tokenizer is None
    assert provider._model is None
