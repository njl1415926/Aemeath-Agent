from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.embeddings.schemas import (
    EmbeddingBatchResult,
    EmbeddingModelSignature,
    EmbeddingRequest,
    ResolvedQueryEmbedding,
)


@runtime_checkable
class EmbeddingProviderPort(Protocol):
    @property
    def signature(self) -> EmbeddingModelSignature:
        ...

    def embed(self, request: EmbeddingRequest) -> EmbeddingBatchResult:
        ...


@runtime_checkable
class QueryEmbeddingSourcePort(Protocol):
    @property
    def signature(self) -> EmbeddingModelSignature:
        ...

    def resolve(self, *, query_text: str) -> ResolvedQueryEmbedding:
        ...
