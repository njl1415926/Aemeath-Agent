from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from app.db.sqlite_store import SQLiteStore
from app.embeddings.artifacts import (
    EmbeddingArtifactWrite,
    canonicalize_embedding_text,
    embedding_content_hash,
)
from app.embeddings.config import load_embedding_space_profile
from app.embeddings.errors import EmbeddingUnavailableError
from app.embeddings.providers.hash import DeterministicHashEmbeddingProvider
from app.embeddings.query import DirectQueryEmbeddingSource
from app.embeddings.schemas import EmbeddingRequest, ResolvedQueryEmbedding
from app.memory.extractor import MemoryExtractionService
from app.memory.observability import record_memory_event
from app.memory.reader import MemoryReader
from app.memory.repair import MemoryEmbeddingRepairService
from app.memory.schemas import MemoryArtifactEligibility, MemoryQuery
from app.memory.worker import MemoryRepairSchedule
from app.observability.schemas import AppLogRecord
from app.schemas import MessageRecord
from app.semantic.coordinator import SemanticQueryCoordinator


class RecordingLogger:
    def __init__(self) -> None:
        self.records: list[AppLogRecord] = []

    def record(self, record: AppLogRecord) -> None:
        self.records.append(record)


class RecordingEmbeddingProvider:
    def __init__(self, *, fail_purpose: str | None = None) -> None:
        self.delegate = DeterministicHashEmbeddingProvider(
            profile=load_embedding_space_profile("deterministic_hash_v1")
        )
        self.fail_purpose = fail_purpose
        self.requests: list[EmbeddingRequest] = []

    @property
    def signature(self):
        return self.delegate.signature

    def embed(self, request: EmbeddingRequest):
        self.requests.append(request)
        if request.purpose == self.fail_purpose:
            raise EmbeddingUnavailableError(reason="test_provider_unavailable")
        return self.delegate.embed(request)


class JsonExtractionModel:
    def __init__(self, memories: list[dict[str, object]]) -> None:
        self.memories = memories

    def chat(self, *, messages: list[dict[str, str]]) -> str:
        assert messages[-1]["role"] == "user"
        return json.dumps({"memories": self.memories}, ensure_ascii=False)


class InvalidVectorQuerySource:
    def __init__(self, signature) -> None:
        self._signature = signature

    @property
    def signature(self):
        return self._signature

    def resolve(self, *, query_text: str) -> ResolvedQueryEmbedding:
        return ResolvedQueryEmbedding(
            signature=self._signature,
            vector=np.zeros(self._signature.dimension, dtype=np.float32),
        )


def _create_memory(
    store,
    *,
    content: str,
    user_id: str = "user_1",
    importance: int = 8,
    confidence: float = 0.9,
    source: str = "test",
    source_message_id: str | None = "message_source",
    source_session_id: str | None = "session_source",
    source_job_id: str | None = "job_source",
) -> str:
    return store.create_memory(
        user_id=user_id,
        scope="user_global",
        scope_character_id=None,
        scope_session_id=None,
        namespace="user_profile",
        memory_type="preference",
        content=content,
        importance=importance,
        confidence=confidence,
        source=source,
        source_message_id=source_message_id,
        source_session_id=source_session_id,
        source_job_id=source_job_id,
        source_message_ids=[source_message_id] if source_message_id else [],
        related_memory_ids=[],
    )


def _write_artifact(store, provider, *, memory_id: str, content: str, signature=None):
    canonical_content = canonicalize_embedding_text(content)
    batch = provider.embed(
        EmbeddingRequest(texts=(canonical_content,), purpose="document")
    )
    artifact = EmbeddingArtifactWrite(
        signature=signature or batch.signature,
        content_hash=embedding_content_hash(canonical_content),
        vector=batch.vectors[0],
    )
    return store.upsert_memory_embedding_artifact(
        memory_id=memory_id,
        user_id="user_1",
        artifact=artifact,
    )


def _message(*, message_id: str = "message_1", content: str = "请记住我喜欢火锅"):
    return MessageRecord(
        id=message_id,
        session_id="session_1",
        user_id="user_1",
        role="user",
        content=content,
        created_at="2026-07-26T00:00:00Z",
        emotion_label=None,
        importance_score=None,
    )


def test_direct_query_source_uses_batch_first_query_purpose() -> None:
    provider = RecordingEmbeddingProvider()

    resolved = DirectQueryEmbeddingSource(provider).resolve(query_text="火锅偏好")

    assert provider.requests == [EmbeddingRequest(texts=("火锅偏好",), purpose="query")]
    assert resolved.signature.canonical() == provider.signature.canonical()
    assert resolved.vector.shape == (provider.signature.dimension,)
    assert np.isclose(np.linalg.norm(resolved.vector), 1.0)


@pytest.mark.parametrize(
    ("query_text", "canonical_text"),
    (
        ("Cafe\u0301", "Café"),
        ("第一行\r\n第二行", "第一行\n第二行"),
    ),
    ids=("nfc", "line_endings"),
)
def test_direct_query_source_canonicalizes_text_like_documents(
    query_text: str,
    canonical_text: str,
) -> None:
    provider = RecordingEmbeddingProvider()
    document = provider.delegate.embed(
        EmbeddingRequest(texts=(canonical_text,), purpose="document")
    )
    provider.requests.clear()

    resolved = DirectQueryEmbeddingSource(provider).resolve(query_text=query_text)

    assert provider.requests == [
        EmbeddingRequest(texts=(canonical_text,), purpose="query")
    ]
    assert np.allclose(resolved.vector, document.vectors[0])


def test_reader_recalls_nfc_equivalent_query(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "reader_nfc.db")
    provider = RecordingEmbeddingProvider()
    memory_id = _create_memory(store, content="Café")
    _write_artifact(store, provider, memory_id=memory_id, content="Café")
    reader = MemoryReader(
        store,
        embedding_provider=provider,
        recall_threshold=0.99,
    )

    results = reader.retrieve_query(
        memory_query=MemoryQuery(
            user_id="user_1",
            character_id="aemeath",
            session_id="session_1",
            query_text="Cafe\u0301",
            limit=1,
        )
    )

    assert [result.record.id for result in results] == [memory_id]
    assert results[0].match_reason == "embedding_similarity"


def test_reader_uses_vector_first_then_keyword_fill_for_partial_coverage(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "reader_partial.db")
    provider = RecordingEmbeddingProvider()
    logger = RecordingLogger()
    vector_id = _create_memory(store, content="用户喜欢火锅")
    keyword_only_id = _create_memory(store, content="用户喜欢火锅，也喜欢烧烤")
    _write_artifact(
        store,
        provider,
        memory_id=vector_id,
        content="用户喜欢火锅",
    )
    provider.requests.clear()
    reader = MemoryReader(
        store,
        embedding_provider=provider,
        recall_top_k=2,
        recall_threshold=0.8,
        app_logger=logger,
    )

    results = reader.retrieve_query(
        memory_query=MemoryQuery(
            user_id="user_1",
            character_id="aemeath",
            session_id="session_1",
            query_text="用户喜欢火锅",
            limit=2,
        )
    )

    assert [result.record.id for result in results] == [vector_id, keyword_only_id]
    assert [result.match_reason for result in results] == [
        "embedding_similarity",
        "keyword_or_recall_fallback",
    ]
    assert provider.requests == [
        EmbeddingRequest(texts=("用户喜欢火锅",), purpose="query")
    ]
    completed = logger.records[-1]
    assert completed.event_name == "memory.recall_completed"
    assert completed.payload["eligible_memory_count"] == 2
    assert completed.payload["compatible_memory_count"] == 1
    assert completed.payload["coverage_complete"] is False
    assert completed.payload["fallback_strategy"] == "keyword_supplement"


@pytest.mark.parametrize("mutation", ("archive", "disable"))
def test_reader_rechecks_active_state_after_vector_hydration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    store = SQLiteStore(tmp_path / f"reader_hydration_{mutation}.db")
    provider = RecordingEmbeddingProvider()
    content = "用户喜欢安静的餐厅"
    memory_id = _create_memory(store, content=content)
    _write_artifact(store, provider, memory_id=memory_id, content=content)
    original_search = store.search_memory_embedding_artifacts

    def search_then_mutate(**kwargs):
        outcome = original_search(**kwargs)
        if mutation == "archive":
            assert store.archive_memory(memory_id=memory_id, user_id="user_1") is True
        else:
            assert store.disable_memory(memory_id=memory_id, user_id="user_1") is True
        return outcome

    monkeypatch.setattr(
        store,
        "search_memory_embedding_artifacts",
        search_then_mutate,
    )
    reader = MemoryReader(
        store,
        embedding_provider=provider,
        recall_threshold=0.99,
    )

    results = reader.retrieve_query(
        memory_query=MemoryQuery(
            user_id="user_1",
            character_id="aemeath",
            session_id="session_1",
            query_text=content,
            limit=1,
        )
    )

    assert results == []
    mutated = store.get_memory(memory_id=memory_id)
    assert mutated is not None
    assert mutated.status == ("archived" if mutation == "archive" else "active")
    assert bool(mutated.is_active) is False
    assert mutated.use_count == 0


def test_reader_keeps_explicit_chinese_active_memory_fallback(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "reader_explicit.db")
    provider = RecordingEmbeddingProvider()
    memory_id = _create_memory(store, content="用户在海州的一家设计公司工作")
    _write_artifact(
        store,
        provider,
        memory_id=memory_id,
        content="用户在海州的一家设计公司工作",
    )
    reader = MemoryReader(
        store,
        embedding_provider=provider,
        recall_threshold=1.1,
    )

    results = reader.retrieve_query(
        memory_query=MemoryQuery(
            user_id="user_1",
            character_id="aemeath",
            session_id="session_1",
            query_text="你还记得之前的事情吗",
            limit=2,
        )
    )

    assert [result.record.id for result in results] == [memory_id]
    assert results[0].match_reason == "keyword_or_recall_fallback"


def test_reader_invalid_shared_query_vector_degrades_without_sensitive_log_data(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "reader_invalid_query.db")
    provider = RecordingEmbeddingProvider()
    logger = RecordingLogger()
    memory_content = "用户喜欢吃麻辣火锅"
    memory_id = _create_memory(store, content=memory_content)
    reader = MemoryReader(
        store,
        query_embedding_source=InvalidVectorQuerySource(provider.signature),
        app_logger=logger,
    )

    results = reader.retrieve_query(
        memory_query=MemoryQuery(
            user_id="user_1",
            character_id="aemeath",
            session_id="session_1",
            query_text=memory_content,
            limit=2,
        )
    )

    assert [result.record.id for result in results] == [memory_id]
    assert logger.records[-1].event_name == "memory.recall_degraded"
    serialized = repr(logger.records)
    assert memory_content not in serialized
    assert "vector" not in serialized


def test_extractor_commits_facts_before_one_document_embedding_batch(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "extract_batch.db")
    provider = RecordingEmbeddingProvider()
    existing_content = "用户喜欢火锅"
    existing_id = _create_memory(
        store,
        content=existing_content,
        importance=3,
        confidence=0.55,
        source="original_source",
        source_message_id="original_message",
        source_session_id="original_session",
        source_job_id="original_job",
    )
    new_content = "用户在海州从事设计工作"
    model = JsonExtractionModel(
        [
            {
                "content": existing_content,
                "memory_type": "preference",
                "importance": 10,
                "confidence": 0.99,
                "source_message_ids": ["message_1"],
            },
            {
                "content": new_content,
                "memory_type": "fact",
                "importance": 7,
                "confidence": 0.9,
                "source_message_ids": ["message_1"],
            },
        ]
    )
    service = MemoryExtractionService(
        store=store,
        model_client=model,  # type: ignore[arg-type]
        embedding_provider=provider,
    )

    result = service.extract_and_persist(
        user_id="user_1",
        session_id="session_1",
        character_id="aemeath",
        messages=[_message()],
        trigger_type="manual",
        source_job_id="job_new",
    )

    assert result.existing_memory_ids == [existing_id]
    assert len(result.created_memory_ids) == 1
    assert set(result.artifact_written_memory_ids) == {
        existing_id,
        result.created_memory_ids[0],
    }
    assert result.embedding_pending_memory_ids == []
    assert provider.requests == [
        EmbeddingRequest(
            texts=(existing_content, new_content),
            purpose="document",
        )
    ]
    preserved = store.get_memory(memory_id=existing_id)
    assert preserved is not None
    assert preserved.importance == 3
    assert preserved.confidence == 0.55
    assert preserved.source == "original_source"
    assert preserved.source_message_id == "original_message"
    assert preserved.source_session_id == "original_session"
    assert preserved.source_job_id == "original_job"


def test_extractor_embedding_failure_preserves_facts_as_repair_gaps(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "extract_pending.db")
    provider = RecordingEmbeddingProvider(fail_purpose="document")
    logger = RecordingLogger()
    memory_content = "用户希望以后少吃甜食"
    service = MemoryExtractionService(
        store=store,
        model_client=JsonExtractionModel(  # type: ignore[arg-type]
            [
                {
                    "content": memory_content,
                    "memory_type": "preference",
                    "source_message_ids": ["message_1"],
                }
            ]
        ),
        embedding_provider=provider,
        app_logger=logger,
    )

    result = service.extract_and_persist(
        user_id="user_1",
        session_id="session_1",
        character_id="aemeath",
        messages=[_message()],
        trigger_type="hard",
        source_job_id="job_pending",
    )

    assert len(result.created_memory_ids) == 1
    assert result.artifact_written_memory_ids == []
    assert result.embedding_pending_memory_ids == result.created_memory_ids
    memory_id = result.created_memory_ids[0]
    assert store.get_memory(memory_id=memory_id) is not None
    gaps = store.list_memory_embedding_artifact_gaps(
        query_signature=provider.signature,
        user_id="user_1",
        limit=10,
    )
    assert [(gap.memory_id, gap.reason) for gap in gaps.gaps] == [
        (memory_id, "missing")
    ]
    assert logger.records[-1].payload["embedding_status"] == "pending"
    assert memory_content not in repr(logger.records)


def test_repair_writes_only_current_signature_and_is_idempotent(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "repair.db")
    provider = RecordingEmbeddingProvider()
    content = "用户习惯在周末晨跑"
    memory_id = _create_memory(store, content=content)
    other_signature = replace(provider.signature, profile_id="other_legal_space")
    _write_artifact(
        store,
        provider,
        memory_id=memory_id,
        content=content,
        signature=other_signature,
    )
    provider.requests.clear()
    service = MemoryEmbeddingRepairService(
        store=store,
        embedding_provider=provider,
    )

    first = service.repair_batch(limit=10, user_id="user_1")
    second = service.repair_batch(limit=10, user_id="user_1")

    assert first.artifact_written_memory_ids == [memory_id]
    assert first.embedding_pending_memory_ids == []
    assert second.gap_count == 0
    assert provider.requests == [
        EmbeddingRequest(texts=(content,), purpose="document")
    ]
    query = provider.delegate.embed(
        EmbeddingRequest(texts=(content,), purpose="query")
    )
    eligibility = MemoryArtifactEligibility(
        user_id="user_1",
        character_id="aemeath",
        session_id="session_1",
    )
    current = store.search_memory_embedding_artifacts(
        eligibility=eligibility,
        query_signature=provider.signature,
        query_embedding=query.vectors[0],
        limit=4,
    )
    other = store.search_memory_embedding_artifacts(
        eligibility=eligibility,
        query_signature=other_signature,
        query_embedding=query.vectors[0],
        limit=4,
    )
    assert [hit.memory_id for hit in current.hits] == [memory_id]
    assert [hit.memory_id for hit in other.hits] == [memory_id]


def test_repair_failure_keeps_cursor_and_schedule_backs_off(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "repair_failure.db")
    provider = RecordingEmbeddingProvider(fail_purpose="document")
    memory_id = _create_memory(store, content="用户每周三晚上学习日语")
    service = MemoryEmbeddingRepairService(
        store=store,
        embedding_provider=provider,
    )

    result = service.repair_batch(limit=1, user_id="user_1")
    schedule = MemoryRepairSchedule(interval_seconds=10, max_backoff_seconds=40)
    schedule.record_result(result, now=100)
    first_next_run = schedule.next_run_at
    second_delay = schedule.record_failure(now=first_next_run)

    assert result.artifact_written_memory_ids == []
    assert result.embedding_pending_memory_ids == [memory_id]
    assert result.next_cursor is None
    assert first_next_run == 110
    assert second_delay == 20
    assert schedule.next_run_at == 130
    schedule.record_result(
        replace(
            result,
            embedding_pending_memory_ids=[],
            error_code=None,
        ),
        now=130,
    )
    assert schedule.current_backoff_seconds == 10
    assert schedule.next_run_at == 140


def test_memory_event_allowlist_drops_sensitive_fields() -> None:
    logger = RecordingLogger()

    record_memory_event(
        logger,
        event_name="memory.test",
        payload={
            "provider": "deterministic_hash",
            "created_memory_count": 1,
            "content": "secret content",
            "query": "secret query",
            "vector": [1.0],
            "hash": "sha256:secret",
            "api_key": "secret-key",
            "provider_body": "secret-body",
        },
    )

    assert logger.records[0].payload == {
        "provider": "deterministic_hash",
        "created_memory_count": 1,
    }


def test_reader_prefers_shared_semantic_vector_without_direct_provider_call(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "reader_shared_semantic.db")
    provider = RecordingEmbeddingProvider()
    logger = RecordingLogger()
    content = "用户喜欢安静的咖啡馆"
    memory_id = _create_memory(store, content=content)
    _write_artifact(store, provider, memory_id=memory_id, content=content)
    coordinator = SemanticQueryCoordinator(
        provider=provider,
        active_roles=("memory_recall",),
    )
    context = coordinator.prepare_turn_context(
        turn_input=_semantic_turn(content, turn_id="turn_shared")
    )
    provider.requests.clear()
    reader = MemoryReader(
        store,
        embedding_provider=provider,
        recall_threshold=0.99,
        app_logger=logger,
    )

    results = reader.retrieve_query(
        memory_query=MemoryQuery(
            user_id="user_1",
            character_id="aemeath",
            session_id="session_1",
            query_text=content,
            limit=1,
        ),
        semantic_context=context,
    )

    assert [result.record.id for result in results] == [memory_id]
    assert provider.requests == []
    usage = next(
        record for record in logger.records if record.event_name == "memory.semantic_query_usage"
    )
    assert usage.turn_id == "turn_shared"
    assert usage.payload == {
        "semantic_role": "memory_recall",
        "semantic_vector_consumed": True,
        "semantic_usage_reason": "vector_search_attempted",
    }


def test_reader_records_ready_vector_as_unused_for_low_information_input(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "reader_shared_unused.db")
    provider = RecordingEmbeddingProvider()
    logger = RecordingLogger()
    context = SemanticQueryCoordinator(
        provider=provider,
        active_roles=("memory_recall",),
    ).prepare_turn_context(turn_input=_semantic_turn("你好", turn_id="turn_unused"))
    provider.requests.clear()
    reader = MemoryReader(store, embedding_provider=provider, app_logger=logger)

    results = reader.retrieve_query(
        memory_query=MemoryQuery(
            user_id="user_1",
            character_id="aemeath",
            session_id="session_1",
            query_text="你好",
        ),
        semantic_context=context,
    )

    assert results == []
    assert provider.requests == []
    assert logger.records[-1].event_name == "memory.semantic_query_usage"
    assert logger.records[-1].turn_id == "turn_unused"
    assert logger.records[-1].payload["semantic_vector_consumed"] is False
    assert logger.records[-1].payload["semantic_usage_reason"] == "low_information_input"


def test_reader_failed_semantic_context_uses_keyword_without_direct_retry(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "reader_shared_failed.db")
    provider = RecordingEmbeddingProvider(fail_purpose="query")
    content = "semantic fallback topic"
    memory_id = _create_memory(store, content=content)
    context = SemanticQueryCoordinator(
        provider=provider,
        active_roles=("memory_recall",),
    ).prepare_turn_context(turn_input=_semantic_turn(content, turn_id="turn_failed"))
    assert context.status == "failed"
    provider.requests.clear()
    logger = RecordingLogger()
    reader = MemoryReader(store, embedding_provider=provider, app_logger=logger)

    results = reader.retrieve_query(
        memory_query=MemoryQuery(
            user_id="user_1",
            character_id="aemeath",
            session_id="session_1",
            query_text=content,
            limit=1,
        ),
        semantic_context=context,
    )

    assert [result.record.id for result in results] == [memory_id]
    assert provider.requests == []
    assert all(
        record.event_name != "memory.semantic_query_usage" for record in logger.records
    )


def _semantic_turn(query: str, *, turn_id: str):
    from app.semantic.schemas import SemanticTurnInput

    return SemanticTurnInput(
        turn_id=turn_id,
        user_id="user_1",
        session_id="session_1",
        character_id="aemeath",
        current_user_message_id="message_current",
        current_input=query,
        recent_messages=(),
    )
