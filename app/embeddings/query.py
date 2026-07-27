from __future__ import annotations

from app.embeddings.artifacts import (
    canonicalize_embedding_text,
    prepare_embedding_query,
)
from app.embeddings.errors import EmbeddingInvalidResponseError
from app.embeddings.ports import EmbeddingProviderPort
from app.embeddings.schemas import (
    EmbeddingModelSignature,
    EmbeddingRequest,
    ResolvedQueryEmbedding,
)


class DirectQueryEmbeddingSource:
    """Resolve one query through the batch-first provider contract."""

    def __init__(self, provider: EmbeddingProviderPort):
        self._provider = provider

    @property
    def signature(self) -> EmbeddingModelSignature:
        return self._provider.signature

    def resolve(self, *, query_text: str) -> ResolvedQueryEmbedding:
        canonical_query = canonicalize_embedding_text(query_text)
        result = self._provider.embed(
            EmbeddingRequest(texts=(canonical_query,), purpose="query")
        )
        if result.signature.canonical() != self.signature.canonical():
            raise EmbeddingInvalidResponseError("query_signature_mismatch")
        if result.vectors.shape[0] != 1:
            raise EmbeddingInvalidResponseError("query_result_count_mismatch")
        prepared = prepare_embedding_query(
            signature=result.signature,
            vector=result.vectors[0],
        )
        return ResolvedQueryEmbedding(
            signature=result.signature,
            vector=prepared.vector.copy(),
        )
