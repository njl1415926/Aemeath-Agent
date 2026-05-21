from __future__ import annotations

from typing import Any

from app.retrieval.ports import RetrieverPort
from app.retrieval.schemas import EvidencePack


class EmptyEvidenceProvider:
    def build_evidence_pack(
        self,
        *,
        user_id: str,
        character_id: str,
        query: str,
        response_mode: str,
        metadata: dict[str, Any] | None = None,
    ) -> EvidencePack:
        return EvidencePack()


class CompositeEvidenceProvider:
    def __init__(
        self,
        *,
        lore_retriever: RetrieverPort | None = None,
        voice_retriever: RetrieverPort | None = None,
    ):
        self.lore_retriever = lore_retriever
        self.voice_retriever = voice_retriever

    def build_evidence_pack(
        self,
        *,
        user_id: str,
        character_id: str,
        query: str,
        response_mode: str,
        metadata: dict[str, Any] | None = None,
    ) -> EvidencePack:
        lore_results = []
        voice_results = []
        if self.lore_retriever:
            lore_results = self.lore_retriever.retrieve(
                query=query,
                user_id=user_id,
                namespaces=["lore", "plot"],
                limit=6,
                metadata={
                    "character_id": character_id,
                    "response_mode": response_mode,
                    **(metadata or {}),
                },
            )
        if self.voice_retriever:
            voice_results = self.voice_retriever.retrieve(
                query=query,
                user_id=user_id,
                namespaces=["voice", "style_examples"],
                limit=4,
                metadata={
                    "character_id": character_id,
                    "response_mode": response_mode,
                    **(metadata or {}),
                },
            )
        return EvidencePack(lore_results=lore_results, voice_results=voice_results)
