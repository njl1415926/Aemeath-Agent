from __future__ import annotations

import json

from app.retrieval.documents import load_flow_chunks, load_wuwa_favor_chunks
from app.retrieval.embedding_retriever import SqliteEmbeddingRetriever
from app.retrieval.embeddings import DeterministicHashEmbeddingProvider
from app.retrieval.provider import WuwaRagEvidenceProvider
from app.retrieval.schemas import RetrievalResult
from app.retrieval.vector_index import SqliteVectorStore


class FakeRetriever:
    def __init__(self):
        self.calls = []

    def retrieve(
        self,
        *,
        query,
        user_id=None,
        namespaces=None,
        limit=8,
        metadata=None,
    ):
        self.calls.append(
            {
                "query": query,
                "user_id": user_id,
                "namespaces": namespaces,
                "limit": limit,
                "metadata": metadata or {},
            }
        )
        namespace = namespaces[0] if namespaces else "canon:lore"
        return [
            RetrievalResult(
                id=f"{namespace}:{index}",
                namespace=namespace,
                content=f"{namespace} evidence {index}",
                source_type="fake",
                confidence=1.0 - index * 0.01,
                metadata={},
            )
            for index in range(limit)
        ]


def test_favor_extract_builds_lore_profile_and_voice_chunks(tmp_path):
    path = tmp_path / "aemeath_favor_extract.json"
    path.write_text(
        json.dumps(
            {
                "files": {
                    "favor_words.json": [
                        {
                            "Id": 121001,
                            "SourceType": "FavorWord",
                            "RoleId": 1210,
                            "SpeakerName": "爱弥斯",
                            "RoleNickName": "爱弥斯",
                            "Title": "心声·一",
                            "Content": "飞行雪绒这个名字，一听就知道是因为雪绒海豹起的吧？",
                            "_source_file": "favor_words.json",
                        }
                    ],
                    "favor_stories.json": [
                        {
                            "Id": 121001,
                            "RoleId": 1210,
                            "SpeakerName": "爱弥斯",
                            "Title": "在雪原上",
                            "Content": "小屋里有游戏卡带。\n\n她想和你一起玩更久。",
                            "_source_file": "favor_stories.json",
                        }
                    ],
                    "favor_role_infos.json": [
                        {
                            "Id": 1210,
                            "RoleId": 1210,
                            "SpeakerName": "爱弥斯",
                            "Info": "星炬学院拉贝尔学部的隧者适格者。",
                            "TalentName": "长航的星辉",
                            "TalentDoc": "现在的我是电子幽灵。",
                            "TalentCertification": "声痕位于胸口。",
                            "_source_file": "favor_role_infos.json",
                        }
                    ],
                    "favor_goods.json": [],
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    chunks = load_wuwa_favor_chunks(path)

    namespaces = [chunk.namespace for chunk in chunks]
    assert "canon:profile" in namespaces
    assert "canon:lore" in namespaces
    assert "canon:voice" in namespaces
    assert any(chunk.source_type == "favor_word_lore" for chunk in chunks)
    assert any(chunk.source_type == "favor_word" for chunk in chunks)


def test_flow_chunks_preserve_file_order_and_build_voice_view(tmp_path):
    path = tmp_path / "flow_texts_tid_sorted.json"
    rows = [
        {
            "StateKey": "state_a",
            "RowType": "Talk",
            "SpeakerName": "爱弥斯",
            "Content": "第二句，但在文件里先出现。",
        },
        {
            "StateKey": "state_a",
            "RowType": "Talk",
            "SpeakerName": "{PlayerName}",
            "Content": "用户回应。",
        },
        {
            "StateKey": "state_a",
            "RowType": "Talk",
            "SpeakerName": "爱弥斯",
            "Content": "第一句，但 TalkItemId 就算更小也不能重排。",
            "TalkItemId": 1,
        },
    ]
    path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")

    chunks = load_flow_chunks(path)
    plot = next(chunk for chunk in chunks if chunk.namespace == "canon:plot")
    voice_chunks = [chunk for chunk in chunks if chunk.namespace == "canon:voice"]

    assert plot.content.index("第二句") < plot.content.index("第一句")
    assert voice_chunks
    assert all(chunk.metadata["derived_view"] == "voice" for chunk in voice_chunks)


def test_wuwa_rag_provider_retrieves_and_deduplicates(tmp_path):
    store = SqliteVectorStore(tmp_path / "rag.sqlite")
    embedder = DeterministicHashEmbeddingProvider()
    chunks_path = tmp_path / "aemeath_favor_extract.json"
    chunks_path.write_text(
        json.dumps(
            {
                "files": {
                    "favor_words.json": [
                        {
                            "Id": 121005,
                            "RoleId": 1210,
                            "SpeakerName": "爱弥斯",
                            "RoleNickName": "爱弥斯",
                            "Title": "心声·五",
                            "Content": "我想成为能和你共同承担责任的救世主。",
                            "_source_file": "favor_words.json",
                        }
                    ],
                    "favor_stories.json": [],
                    "favor_role_infos.json": [],
                    "favor_goods.json": [],
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    chunks = load_wuwa_favor_chunks(chunks_path)
    store.upsert_chunks(
        chunks=chunks,
        embeddings=embedder.embed_texts([chunk.content for chunk in chunks]),
        embedding_model=embedder.model_name,
    )
    retriever = SqliteEmbeddingRetriever(store=store, embedding_provider=embedder)
    provider = WuwaRagEvidenceProvider(retriever=retriever)

    pack = provider.build_evidence_pack(
        user_id="user_default",
        character_id="aemeath",
        metadata={"retrieval_intent": "lore"},
        query="你还想成为救世主吗？",
    )

    assert pack.lore_results
    assert not pack.voice_results
    assert pack.lore_results[0].metadata["retrieval_intent"] == "forced_lore"
    assert "policy_reason" in pack.lore_results[0].metadata
    assert "lore_score" in pack.lore_results[0].metadata
    assert "voice_score" in pack.lore_results[0].metadata
    assert "no_retrieval_score" in pack.lore_results[0].metadata


def test_wuwa_rag_policy_skips_daily_short_inputs():
    retriever = FakeRetriever()
    provider = WuwaRagEvidenceProvider(retriever=retriever)

    for query in ["早呀", "我好累"]:
        pack = provider.build_evidence_pack(
            user_id="user_default",
            character_id="aemeath",
            query=query,
        )

        assert pack.lore_results == []
        assert pack.voice_results == []

    assert retriever.calls == []


def test_wuwa_rag_policy_routes_daily_voice_request():
    retriever = FakeRetriever()
    provider = WuwaRagEvidenceProvider(retriever=retriever)

    pack = provider.build_evidence_pack(
        user_id="user_default",
        character_id="aemeath",
        query="我好累，安慰我",
    )

    assert pack.lore_results == []
    assert len(pack.voice_results) == 2
    assert retriever.calls[0]["namespaces"] == ["canon:voice"]
    assert retriever.calls[0]["metadata"]["retrieval_intent"] == "query_voice"
    assert retriever.calls[0]["metadata"]["voice_score"] >= 3


def test_wuwa_rag_policy_routes_daily_lore_requests():
    retriever = FakeRetriever()
    provider = WuwaRagEvidenceProvider(retriever=retriever)

    for query in ["爱弥斯为什么想成为救世主？", "黎那汐塔是什么地方？"]:
        pack = provider.build_evidence_pack(
            user_id="user_default",
            character_id="aemeath",
            query=query,
        )

        assert len(pack.lore_results) == 5
        assert pack.voice_results == []

    assert all(
        call["namespaces"] == ["canon:profile", "canon:lore", "canon:plot"]
        for call in retriever.calls
    )
    assert all(call["metadata"]["retrieval_intent"] == "query_lore" for call in retriever.calls)


def test_wuwa_rag_policy_routes_explicit_voice_request():
    retriever = FakeRetriever()
    provider = WuwaRagEvidenceProvider(retriever=retriever)

    pack = provider.build_evidence_pack(
        user_id="user_default",
        character_id="aemeath",
        query="用爱弥斯的语气安慰我",
    )

    assert pack.lore_results == []
    assert len(pack.voice_results) == 2
    assert retriever.calls[0]["metadata"]["retrieval_intent"] == "query_voice"


def test_wuwa_rag_policy_routes_lore_and_voice_request():
    retriever = FakeRetriever()
    provider = WuwaRagEvidenceProvider(retriever=retriever)

    pack = provider.build_evidence_pack(
        user_id="user_default",
        character_id="aemeath",
        query="爱弥斯知道漂泊者过去后会怎么安慰他？",
    )

    assert len(pack.lore_results) == 5
    assert len(pack.voice_results) == 3
    assert retriever.calls[0]["namespaces"] == ["canon:profile", "canon:lore", "canon:plot"]
    assert retriever.calls[1]["namespaces"] == ["canon:voice"]
    assert retriever.calls[0]["metadata"]["retrieval_intent"] == "query_lore_and_voice"
    assert retriever.calls[1]["metadata"]["retrieval_intent"] == "query_lore_and_voice"


def test_wuwa_rag_policy_supports_debug_retrieval_intent_overrides():
    cases = [
        ("none", 0, 0, 0),
        ("lore", 1, 5, 0),
        ("voice", 1, 0, 2),
        ("lore_and_voice", 2, 5, 3),
    ]

    for intent, call_count, lore_count, voice_count in cases:
        retriever = FakeRetriever()
        provider = WuwaRagEvidenceProvider(retriever=retriever)
        pack = provider.build_evidence_pack(
            user_id="user_default",
            character_id="aemeath",
            query="早呀",
            metadata={"retrieval_intent": intent},
        )

        assert len(retriever.calls) == call_count
        assert len(pack.lore_results) == lore_count
        assert len(pack.voice_results) == voice_count
