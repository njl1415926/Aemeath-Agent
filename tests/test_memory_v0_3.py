from __future__ import annotations

import json
from dataclasses import replace

import pytest

from app.embeddings.artifacts import (
    EmbeddingArtifactWrite,
    canonicalize_embedding_text,
    embedding_content_hash,
)
from app.embeddings.schemas import EmbeddingRequest
from app.main import build_controller
from app.memory.admin import MemoryAdminService
from app.memory.extractor import MemoryExtractionError, MemoryExtractionService
from app.memory.jobs import build_memory_job_idempotency_key
from app.memory.schemas import MemoryArtifactEligibility
from app.retrieval.embeddings import DeterministicHashEmbeddingProvider
from app.schemas import AgentTurnInput, MessageRecord
from app.utils.ids import new_id


class JsonMemoryModel:
    def __init__(self, payload: str):
        self.payload = payload

    def chat(self, *, messages: list[dict[str, str]]) -> str:
        return self.payload


def _message(message_id: str, *, role: str, content: str) -> MessageRecord:
    return MessageRecord(
        id=message_id,
        session_id="session_test",
        user_id="user_default",
        role=role,
        content=content,
        created_at="2026-01-01T00:00:00",
        emotion_label=None,
        importance_score=None,
    )


def test_memory_extraction_job_enqueue_is_idempotent(controller_and_store):
    _controller, store, settings = controller_and_store
    source_ids = ["msg_1"]
    key = build_memory_job_idempotency_key(
        user_id=settings.default_user_id,
        character_id=settings.default_character_id,
        session_id="session_1",
        trigger_type="hard",
        source_message_ids=source_ids,
    )

    first = store.enqueue_memory_extraction_job(
        idempotency_key=key,
        user_id=settings.default_user_id,
        session_id="session_1",
        character_id=settings.default_character_id,
        trigger_type="hard",
        source_message_ids=source_ids,
        input_payload={"messages": []},
    )
    second = store.enqueue_memory_extraction_job(
        idempotency_key=key,
        user_id=settings.default_user_id,
        session_id="session_1",
        character_id=settings.default_character_id,
        trigger_type="hard",
        source_message_ids=source_ids,
        input_payload={"messages": []},
    )

    assert first.created is True
    assert second.created is False
    assert first.job.id == second.job.id


def test_memory_extraction_job_claim_and_complete(controller_and_store):
    _controller, store, settings = controller_and_store
    key = build_memory_job_idempotency_key(
        user_id=settings.default_user_id,
        character_id=settings.default_character_id,
        session_id="session_1",
        trigger_type="hard",
        source_message_ids=["msg_1"],
    )
    job = store.enqueue_memory_extraction_job(
        idempotency_key=key,
        user_id=settings.default_user_id,
        session_id="session_1",
        character_id=settings.default_character_id,
        trigger_type="hard",
        source_message_ids=["msg_1"],
        input_payload={"messages": []},
    ).job

    claimed = store.claim_next_memory_extraction_job(worker_id="worker_1")
    second_claim = store.claim_next_memory_extraction_job(worker_id="worker_2")
    assert claimed is not None
    assert claimed.id == job.id
    assert claimed.status == "running"
    assert second_claim is None

    store.complete_memory_extraction_job(
        job_id=job.id,
        result_payload={"created_memory_ids": ["memory_1"]},
    )
    [completed] = store.list_memory_extraction_jobs(session_id="session_1")
    assert completed.status == "completed"
    assert completed.result_payload == {"created_memory_ids": ["memory_1"]}


def test_memory_extraction_service_writes_memory_and_embedding(controller_and_store):
    _controller, store, settings = controller_and_store
    service = MemoryExtractionService(
        store=store,
        model_client=JsonMemoryModel(
            json.dumps(
                {
                    "memories": [
                        {
                            "content": "用户喜欢吃火锅，尤其是辣锅。",
                            "memory_type": "preference",
                            "importance": 8,
                            "confidence": 0.92,
                            "source_message_ids": ["msg_1"],
                            "reason": "用户明确表达饮食偏好",
                        }
                    ]
                },
                ensure_ascii=False,
            )
        ),
        embedding_provider=DeterministicHashEmbeddingProvider(),
    )

    result = service.extract_and_persist(
        user_id=settings.default_user_id,
        session_id="session_test",
        character_id=settings.default_character_id,
        messages=[_message("msg_1", role="user", content="记住，我喜欢吃火锅，尤其是辣锅。")],
        trigger_type="manual",
    )

    assert len(result.created_memory_ids) == 1
    assert result.existing_memory_ids == []
    assert result.artifact_written_memory_ids == result.created_memory_ids
    assert result.embedding_pending_memory_ids == []
    memory = store.get_memory(memory_id=result.created_memory_ids[0])
    assert memory is not None
    assert memory.content == "用户喜欢吃火锅，尤其是辣锅。"
    assert memory.scope == "user_global"
    assert memory.source_session_id == "session_test"
    assert memory.source_job_id is None
    assert memory.source_message_ids == ["msg_1"]
    assert json.loads(memory.related_memory_ids or "[]") == []
    provider = DeterministicHashEmbeddingProvider()
    query = provider.embed(EmbeddingRequest(texts=("出去吃火锅",), purpose="query"))
    outcome = store.search_memory_embedding_artifacts(
        eligibility=MemoryArtifactEligibility(
            user_id=settings.default_user_id,
            character_id=settings.default_character_id,
            session_id="session_test",
        ),
        query_signature=query.signature,
        query_embedding=query.vectors[0],
        limit=4,
    )
    assert outcome.hits[0].memory_id == memory.id


def test_memory_extraction_service_rejects_malformed_json(controller_and_store):
    _controller, store, _settings = controller_and_store
    service = MemoryExtractionService(
        store=store,
        model_client=JsonMemoryModel("不是 JSON"),
        embedding_provider=DeterministicHashEmbeddingProvider(),
    )

    with pytest.raises(MemoryExtractionError):
        service.extract_memories(
            user_id="user_default",
            session_id="session_test",
            character_id="aemeath",
            messages=[_message("msg_1", role="user", content="记住，我喜欢火锅。")],
            trigger_type="manual",
        )


def test_mock_memory_extraction_model_supports_manual_extract(settings):
    settings = replace(settings, enable_long_term_memory=True)
    runtime = build_controller(settings=settings)
    controller = runtime.agent_service.controller
    memory_admin, resolved_settings, store = (
        runtime.memory_admin_service,
        runtime.settings,
        runtime.store,
    )
    store.ensure_session(
        session_id="session_mock",
        user_id=resolved_settings.default_user_id,
        character_id=resolved_settings.default_character_id,
    )
    controller.message_store.save_message(
        session_id="session_mock",
        user_id=resolved_settings.default_user_id,
        role="user",
        content="记住，我喜欢吃火锅。",
    )

    output = memory_admin.handle_command(
        command="/memory extract 1",
        user_id=resolved_settings.default_user_id,
        session_id="session_mock",
        character_id="aemeath",
    )

    assert "created" in output
    memories = store.list_active_memories(user_id=resolved_settings.default_user_id)
    assert memories


def test_memory_admin_extract_handles_disabled_service(controller_and_store):
    _controller, store, settings = controller_and_store
    admin = MemoryAdminService(store, session_store=store)

    output = admin.handle_command(
        command="/memory extract 6",
        user_id=settings.default_user_id,
        session_id="session_test",
        character_id=settings.default_character_id,
    )

    assert output == "long-term memory extraction is disabled"


def test_window_extraction_enqueues_after_twelve_messages(long_term_memory_controller_and_store):
    controller, store, settings = long_term_memory_controller_and_store
    session_id = new_id("session")

    for index in range(6):
        controller.handle_user_message(
            AgentTurnInput(
                user_id=settings.default_user_id,
                session_id=session_id,
                character_id=settings.default_character_id,
                message=f"普通对话 {index}",
                metadata={},
            )
        )

    jobs = store.list_memory_extraction_jobs(session_id=session_id)
    window_jobs = [job for job in jobs if job.trigger_type == "window"]
    assert len(window_jobs) == 1
    assert len(window_jobs[0].source_message_ids) == 12


def test_window_extraction_can_be_disabled(settings):
    settings = replace(
        settings,
        enable_long_term_memory=True,
        enable_memory_window_extraction=False,
    )
    runtime = build_controller(settings=settings)
    controller, resolved_settings, store = (
        runtime.agent_service.controller,
        runtime.settings,
        runtime.store,
    )
    session_id = new_id("session")

    for index in range(6):
        controller.handle_user_message(
            AgentTurnInput(
                user_id=resolved_settings.default_user_id,
                session_id=session_id,
                character_id=resolved_settings.default_character_id,
                message=f"普通对话 {index}",
                metadata={},
            )
        )

    assert store.list_memory_extraction_jobs(session_id=session_id) == []


def test_prompt_log_records_injected_memory_ids(settings):
    settings = replace(
        settings,
        enable_long_term_memory=True,
        enable_prompt_run_log=True,
        prompt_run_log_root=settings.db_path.parent / ".aemeath" / "sessions",
    )
    runtime = build_controller(settings=settings)
    controller, resolved_settings, store = (
        runtime.agent_service.controller,
        runtime.settings,
        runtime.store,
    )
    memory_id = store.create_memory(
        user_id=resolved_settings.default_user_id,
        scope="user_global",
        scope_character_id=None,
        scope_session_id=None,
        namespace="user_profile",
        memory_type="preference",
        content="用户喜欢吃火锅，尤其是辣锅。",
        importance=8,
        confidence=0.9,
        source="test",
        source_message_id=None,
        source_session_id=None,
        source_job_id=None,
        source_message_ids=[],
        related_memory_ids=[],
    )
    embedder = DeterministicHashEmbeddingProvider()
    canonical_content = canonicalize_embedding_text("用户喜欢吃火锅，尤其是辣锅。")
    document = embedder.embed(
        EmbeddingRequest(texts=(canonical_content,), purpose="document")
    )
    store.upsert_memory_embedding_artifact(
        memory_id=memory_id,
        user_id=resolved_settings.default_user_id,
        artifact=EmbeddingArtifactWrite(
            signature=document.signature,
            content_hash=embedding_content_hash(canonical_content),
            vector=document.vectors[0],
        ),
    )

    controller.handle_user_message(
        AgentTurnInput(
            user_id=resolved_settings.default_user_id,
            session_id=new_id("session"),
            character_id=resolved_settings.default_character_id,
            message="用户喜欢吃火锅，尤其是辣锅。",
            metadata={},
        )
    )

    [log_file] = list(resolved_settings.prompt_run_log_root.glob("*/*/*/*.jsonl"))
    record = json.loads(log_file.read_text(encoding="utf-8"))
    assert record["prompt_context_metadata"]["memory_ids"] == [memory_id]
    memory_sections = [
        section for section in record["sections"] if section["id"] == "interaction_memories"
    ]
    assert memory_sections[0]["source_ids"] == [memory_id]
