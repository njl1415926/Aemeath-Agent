from __future__ import annotations

import math
import warnings
from typing import Protocol

import numpy as np

from app.embeddings.errors import EmbeddingInvalidInputError
from app.embeddings.ports import EmbeddingProviderPort
from app.embeddings.schemas import EmbeddingModelSignature, EmbeddingRequest


class LegacyEmbeddingProviderPort(Protocol):
    model_name: str
    dimension: int

    def embed_texts(self, texts: list[str]) -> list[np.ndarray]:
        ...

    def embed_query(self, text: str) -> np.ndarray:
        ...


class LegacyEmbeddingMethodsMixin:
    _legacy_model_name: str

    @property
    def signature(self) -> EmbeddingModelSignature:
        raise NotImplementedError

    def embed(self, request: EmbeddingRequest):
        raise NotImplementedError

    @property
    def model_name(self) -> str:
        return self._legacy_model_name

    @property
    def dimension(self) -> int:
        return self.signature.dimension

    def embed_texts(self, texts: list[str]) -> list[np.ndarray]:
        result = self.embed(EmbeddingRequest(texts=tuple(texts), purpose="document"))
        return [row.copy() for row in result.vectors]

    def embed_query(self, text: str) -> np.ndarray:
        result = self.embed(EmbeddingRequest(texts=(text,), purpose="query"))
        return result.vectors[0].copy()


class LegacyEmbeddingProviderAdapter(LegacyEmbeddingMethodsMixin):
    def __init__(self, provider: EmbeddingProviderPort, *, model_name: str | None = None):
        self._provider = provider
        self._legacy_model_name = model_name or provider.signature.model_id

    @property
    def signature(self) -> EmbeddingModelSignature:
        return self._provider.signature

    def embed(self, request: EmbeddingRequest):
        return self._provider.embed(request)


def warn_legacy_embedding_api(name: str) -> None:
    warnings.warn(
        f"{name} is a compatibility API; use app.embeddings batch-first interfaces.",
        DeprecationWarning,
        stacklevel=3,
    )


def embed_one(
    provider: EmbeddingProviderPort,
    text: str,
    *,
    purpose: str = "query",
) -> np.ndarray:
    if purpose not in {"query", "document"}:
        raise EmbeddingInvalidInputError("unsupported_purpose")
    result = provider.embed(EmbeddingRequest(texts=(text,), purpose=purpose))  # type: ignore[arg-type]
    return result.vectors[0].copy()


def cosine_similarity(left: np.ndarray, right: np.ndarray) -> float:
    left_norm = float(np.linalg.norm(left))
    right_norm = float(np.linalg.norm(right))
    if math.isclose(left_norm, 0.0) or math.isclose(right_norm, 0.0):
        return 0.0
    return float(np.dot(left, right) / (left_norm * right_norm))
