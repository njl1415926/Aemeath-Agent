from __future__ import annotations

from app.retrieval.embeddings import DeterministicHashEmbeddingProvider
from app.schemas import AgentTurnInput
from app.utils.ids import new_id


def test_memory_recall_uses_vector_indexed_memory(long_term_memory_controller_and_store):
    controller, store, settings = long_term_memory_controller_and_store
    session_id = new_id("session")

    memory_id = store.create_memory(
        user_id=settings.default_user_id,
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
    store.upsert_memory_embedding(
        memory_id=memory_id,
        user_id=settings.default_user_id,
        embedding_model=embedder.model_name,
        embedding=embedder.embed_query("用户喜欢吃火锅，尤其是辣锅。"),
    )

    output = controller.handle_user_message(
        AgentTurnInput(
            user_id=settings.default_user_id,
            session_id=session_id,
            character_id=settings.default_character_id,
            message="用户喜欢吃火锅，尤其是辣锅。",
            metadata={},
        )
    )
    assert output.used_memory_ids == [memory_id]
    assert output.used_memories[0].content == "用户喜欢吃火锅，尤其是辣锅。"

    memories = store.list_active_memories(user_id=settings.default_user_id)
    assert max(memory.use_count for memory in memories) >= 1
