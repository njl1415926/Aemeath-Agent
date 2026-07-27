from __future__ import annotations

from app.embeddings.config import load_embedding_space_profile
from app.embeddings.providers.hash import DeterministicHashEmbeddingProvider
from app.embeddings.schemas import EmbeddingRequest
from app.memory.repair import MemoryEmbeddingRepairService
from app.memory.schemas import MemoryArtifactEligibility


def test_memory_repair_runtime_contract_is_idempotent_across_backends(store) -> None:
    provider = DeterministicHashEmbeddingProvider(
        profile=load_embedding_space_profile("deterministic_hash_v1")
    )
    content = "用户偏好安静的餐厅"
    memory_id = store.create_memory(
        user_id="user_1",
        scope="user_global",
        scope_character_id=None,
        scope_session_id=None,
        namespace="user_profile",
        memory_type="preference",
        content=content,
        importance=8,
        confidence=0.9,
        source="test",
        source_message_id="message_1",
        source_session_id="session_1",
        source_job_id="job_1",
        source_message_ids=["message_1"],
        related_memory_ids=[],
    )
    service = MemoryEmbeddingRepairService(
        store=store,
        embedding_provider=provider,
    )

    first = service.repair_batch(limit=4, user_id="user_1")
    second = service.repair_batch(limit=4, user_id="user_1")

    assert first.artifact_written_memory_ids == [memory_id]
    assert first.embedding_pending_memory_ids == []
    assert second.gap_count == 0
    query = provider.embed(EmbeddingRequest(texts=(content,), purpose="query"))
    outcome = store.search_memory_embedding_artifacts(
        eligibility=MemoryArtifactEligibility(
            user_id="user_1",
            character_id="aemeath",
            session_id="session_1",
        ),
        query_signature=query.signature,
        query_embedding=query.vectors[0],
        limit=4,
    )
    assert [hit.memory_id for hit in outcome.hits] == [memory_id]
