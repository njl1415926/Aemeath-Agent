from __future__ import annotations

from app.config import Settings
from app.retrieval.embeddings import (
    BgeM3EmbeddingProvider,
    DeterministicHashEmbeddingProvider,
    EmbeddingProviderPort,
)


def build_memory_embedding_provider(settings: Settings) -> EmbeddingProviderPort:
    provider = settings.memory_embedding_provider.strip().lower()
    if provider == "hash":
        return DeterministicHashEmbeddingProvider()
    if provider in {"bge-m3", "bge_m3", "bge"}:
        return BgeM3EmbeddingProvider(model_path=settings.memory_embedding_model_path)
    raise ValueError(
        "AEMEATH_MEMORY_EMBEDDING_PROVIDER must be one of: bge-m3, hash."
    )
