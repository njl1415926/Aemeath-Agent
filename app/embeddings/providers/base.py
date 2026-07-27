from __future__ import annotations

from app.embeddings.compat import LegacyEmbeddingMethodsMixin
from app.embeddings.schemas import EmbeddingModelSignature


class BaseEmbeddingProvider(LegacyEmbeddingMethodsMixin):
    def __init__(self, *, signature: EmbeddingModelSignature, legacy_model_name: str):
        self._signature = signature
        self._legacy_model_name = legacy_model_name

    @property
    def signature(self) -> EmbeddingModelSignature:
        return self._signature
