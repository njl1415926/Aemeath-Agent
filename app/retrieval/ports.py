from __future__ import annotations

from typing import Any, Protocol

from app.retrieval.schemas import EvidencePack, RetrievalResult
from app.semantic.schemas import TurnSemanticContext


class RetrieverPort(Protocol):
    def retrieve(
        self,
        *,
        query: str,
        user_id: str | None = None,
        namespaces: list[str] | None = None,
        limit: int = 8,
        metadata: dict[str, Any] | None = None,
    ) -> list[RetrievalResult]:
        ...


class EvidenceProviderPort(Protocol):
    def build_evidence_pack(
        self,
        *,
        user_id: str,
        character_id: str,
        query: str,
        metadata: dict[str, Any] | None = None,
        semantic_context: TurnSemanticContext | None = None,
    ) -> EvidencePack:
        ...
