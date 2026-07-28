from __future__ import annotations

from threading import RLock

from app.embeddings.compat import LegacyEmbeddingMethodsMixin
from app.embeddings.ports import EmbeddingProviderPort
from app.embeddings.schemas import EmbeddingBatchResult, EmbeddingModelSignature, EmbeddingRequest


class SynchronizedEmbeddingProvider(LegacyEmbeddingMethodsMixin):
    """Serialize complete calls to a shared in-process embedding provider."""

    def __init__(self, provider: EmbeddingProviderPort):
        self._provider = provider
        self._lock = RLock()
        legacy_name = getattr(provider, "model_name", provider.signature.model_id)
        self._legacy_model_name = (
            legacy_name if isinstance(legacy_name, str) else provider.signature.model_id
        )

    @property
    def signature(self) -> EmbeddingModelSignature:
        return self._provider.signature

    def embed(self, request: EmbeddingRequest) -> EmbeddingBatchResult:
        with self._lock:
            return self._provider.embed(request)
