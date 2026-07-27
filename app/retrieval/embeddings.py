"""Deprecated Retrieval import surface for the neutral embedding foundation.

The implementation lives only in ``app.embeddings``. This module remains for one
compatibility release because existing Memory/RAG callers still use its single-item
helpers and legacy provider protocol.
"""

from app.embeddings.compat import (
    LegacyEmbeddingProviderPort as EmbeddingProviderPort,
)
from app.embeddings.compat import (
    cosine_similarity,
)
from app.embeddings.providers.hash import DeterministicHashEmbeddingProvider
from app.embeddings.providers.local_bge_m3 import BgeM3EmbeddingProvider

__all__ = [
    "BgeM3EmbeddingProvider",
    "DeterministicHashEmbeddingProvider",
    "EmbeddingProviderPort",
    "cosine_similarity",
]
