from __future__ import annotations

from typing import Any

from app.retrieval.provider import CompositeEvidenceProvider, EmptyEvidenceProvider
from app.retrieval.schemas import RetrievalResult


class FakeRetriever:
    def __init__(self, source_type: str):
        self.source_type = source_type
        self.calls: list[dict[str, Any]] = []

    def retrieve(
        self,
        *,
        query: str,
        user_id: str | None = None,
        namespaces: list[str] | None = None,
        limit: int = 8,
        metadata: dict[str, Any] | None = None,
    ) -> list[RetrievalResult]:
        self.calls.append(
            {
                "query": query,
                "user_id": user_id,
                "namespaces": namespaces,
                "limit": limit,
                "metadata": metadata,
            }
        )
        return [
            RetrievalResult(
                id=f"{self.source_type}_1",
                namespace=namespaces[0] if namespaces else self.source_type,
                content=f"{self.source_type} evidence",
                source_type=self.source_type,
                confidence=0.9,
            )
        ]


def test_empty_evidence_provider_returns_empty_pack():
    provider = EmptyEvidenceProvider()

    pack = provider.build_evidence_pack(
        user_id="user_default",
        character_id="aemeath",
        query="hello",
        response_mode="casual_chat",
    )

    assert pack.lore_results == []
    assert pack.voice_results == []


def test_composite_evidence_provider_routes_lore_and_voice_retrievers():
    lore = FakeRetriever("lore")
    voice = FakeRetriever("voice")
    provider = CompositeEvidenceProvider(lore_retriever=lore, voice_retriever=voice)

    pack = provider.build_evidence_pack(
        user_id="user_default",
        character_id="aemeath",
        query="Aemeath",
        response_mode="lore_answer",
        metadata={"trace_id": "trace_1"},
    )

    assert [item.source_type for item in pack.lore_results] == ["lore"]
    assert [item.source_type for item in pack.voice_results] == ["voice"]
    assert lore.calls[0]["namespaces"] == ["lore", "plot"]
    assert voice.calls[0]["namespaces"] == ["voice", "style_examples"]
    assert lore.calls[0]["metadata"]["character_id"] == "aemeath"
    assert lore.calls[0]["metadata"]["trace_id"] == "trace_1"
