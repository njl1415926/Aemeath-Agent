from __future__ import annotations

import json

import numpy as np

from app.retrieval.embeddings import DeterministicHashEmbeddingProvider


def _create_memory(
    store,
    *,
    user_id: str = "user_1",
    content: str,
    scope: str = "user_global",
    scope_character_id: str | None = None,
    scope_session_id: str | None = None,
) -> str:
    return store.create_memory(
        user_id=user_id,
        scope=scope,
        scope_character_id=scope_character_id,
        scope_session_id=scope_session_id,
        namespace="user_profile",
        memory_type="preference",
        content=content,
        importance=8,
        confidence=0.9,
        source="test",
        source_message_id=None,
        source_session_id="source_session",
        source_job_id="source_job",
        source_message_ids=["msg_1"],
        related_memory_ids=["memory_related"],
    )


def test_memory_scope_visibility_and_exact_dedupe_contract(store):
    global_id = _create_memory(store, content="scope-hit global")
    character_a_id = _create_memory(
        store,
        content="scope-hit character-a",
        scope="character",
        scope_character_id="aemeath",
    )
    _create_memory(
        store,
        content="scope-hit character-b",
        scope="character",
        scope_character_id="other",
    )
    session_a_id = _create_memory(
        store,
        content="scope-hit session-a",
        scope="session",
        scope_character_id="aemeath",
        scope_session_id="session_a",
    )
    _create_memory(
        store,
        content="scope-hit session-b",
        scope="session",
        scope_character_id="aemeath",
        scope_session_id="session_b",
    )

    visible_ids = {
        memory.id
        for memory in store.search_memories_keyword(
            user_id="user_1",
            character_id="aemeath",
            session_id="session_a",
            keywords=["scope-hit"],
            limit=10,
        )
    }
    assert visible_ids == {global_id, character_a_id, session_a_id}
    fallback_ids = {
        memory.id
        for memory in store.list_recallable_memories(
            user_id="user_1",
            character_id="aemeath",
            session_id="session_a",
            limit=10,
        )
    }
    assert fallback_ids == visible_ids

    assert store.has_exact_active_memory(
        user_id="user_1",
        scope="user_global",
        scope_character_id=None,
        scope_session_id=None,
        content="scope-hit global",
    )
    assert not store.has_exact_active_memory(
        user_id="user_1",
        scope="session",
        scope_character_id="aemeath",
        scope_session_id="session_b",
        content="scope-hit session-a",
    )

    loaded = store.get_memory(memory_id=global_id)
    assert loaded is not None
    assert loaded.source_session_id == "source_session"
    assert loaded.source_job_id == "source_job"
    assert loaded.source_message_ids == ["msg_1"]
    assert json.loads(loaded.related_memory_ids or "[]") == ["memory_related"]


def test_memory_owner_only_archive_disable_and_usage_contract(store):
    archive_id = _create_memory(store, content="owner-only archive")
    disable_id = _create_memory(store, content="owner-only disable")

    assert store.archive_memory(memory_id=archive_id, user_id="user_2") is False
    assert store.disable_memory(memory_id=disable_id, user_id="user_2") is False
    assert store.get_memory(memory_id=archive_id).status == "active"
    assert store.get_memory(memory_id=disable_id).is_active == 1

    store.mark_memory_used(memory_id=archive_id)
    assert store.get_memory(memory_id=archive_id).use_count == 1
    assert store.get_memory(memory_id=archive_id).last_used_at is not None

    assert store.archive_memory(memory_id=archive_id, user_id="user_1") is True
    assert store.disable_memory(memory_id=disable_id, user_id="user_1") is True
    assert store.get_memory(memory_id=archive_id).status == "archived"
    assert store.get_memory(memory_id=disable_id).is_active == 0


def test_memory_job_and_window_message_contract(store):
    session = store.create_session(user_id="user_1", character_id="aemeath")
    message_ids = [
        store.save_message(
            session_id=session.id,
            user_id="user_1",
            role="user",
            content=f"message {index}",
        )
        for index in range(3)
    ]

    first = store.enqueue_memory_extraction_job(
        idempotency_key="job_key",
        user_id="user_1",
        session_id=session.id,
        character_id="aemeath",
        trigger_type="window",
        source_message_ids=message_ids[:2],
        input_payload={"messages": []},
    )
    second = store.enqueue_memory_extraction_job(
        idempotency_key="job_key",
        user_id="user_1",
        session_id=session.id,
        character_id="aemeath",
        trigger_type="window",
        source_message_ids=message_ids[:2],
        input_payload={"messages": []},
    )
    assert first.created is True
    assert second.created is False
    assert first.job.id == second.job.id

    unprocessed = store.list_unprocessed_window_messages(session_id=session.id, limit=10)
    assert [message.id for message in unprocessed] == [message_ids[2]]

    claimed = store.claim_next_memory_extraction_job(worker_id="worker_1")
    assert claimed is not None
    assert claimed.status == "running"
    assert claimed.attempt_count == 1
    assert store.claim_next_memory_extraction_job(worker_id="worker_2") is None

    store.complete_memory_extraction_job(
        job_id=claimed.id,
        result_payload={"created_memory_ids": ["memory_1"]},
    )
    [completed] = store.list_memory_extraction_jobs(session_id=session.id)
    assert completed.status == "completed"
    assert completed.result_payload == {"created_memory_ids": ["memory_1"]}

    failed = store.enqueue_memory_extraction_job(
        idempotency_key="job_key_failed",
        user_id="user_1",
        session_id=session.id,
        character_id="aemeath",
        trigger_type="hard",
        source_message_ids=[],
        input_payload={},
    ).job
    store.fail_memory_extraction_job(job_id=failed.id, error="boom")
    failed_job = [
        job for job in store.list_memory_extraction_jobs(session_id=session.id)
        if job.id == failed.id
    ][0]
    assert failed_job.status == "failed"
    assert failed_job.error == "boom"


def test_memory_embedding_storage_and_visibility_contract(store):
    visible_id = _create_memory(store, content="vector visible")
    hidden_id = _create_memory(
        store,
        content="vector hidden",
        scope="session",
        scope_character_id="aemeath",
        scope_session_id="other_session",
    )
    embedder = DeterministicHashEmbeddingProvider()
    for memory_id, text in [
        (visible_id, "vector visible"),
        (hidden_id, "vector hidden"),
    ]:
        store.upsert_memory_embedding(
            memory_id=memory_id,
            user_id="user_1",
            embedding_model=embedder.model_name,
            embedding=embedder.embed_query(text),
        )

    hits = store.search_memory_embeddings(
        user_id="user_1",
        character_id="aemeath",
        session_id="session_a",
        query_embedding=embedder.embed_query("vector visible"),
        limit=4,
    )
    assert [hit.memory_id for hit in hits] == [visible_id]


def test_memory_embedding_shape_mismatch_is_skipped_contract(store):
    good_id = _create_memory(store, content="vector good")
    malformed_id = _create_memory(store, content="vector malformed")
    embedder = DeterministicHashEmbeddingProvider()
    store.upsert_memory_embedding(
        memory_id=good_id,
        user_id="user_1",
        embedding_model=embedder.model_name,
        embedding=embedder.embed_query("vector good"),
    )
    store.upsert_memory_embedding(
        memory_id=malformed_id,
        user_id="user_1",
        embedding_model=embedder.model_name,
        embedding=np.array([1.0, 0.0], dtype=np.float32),
    )

    hits = store.search_memory_embeddings(
        user_id="user_1",
        character_id="aemeath",
        session_id="session_a",
        query_embedding=embedder.embed_query("vector good"),
        limit=4,
    )
    assert good_id in {hit.memory_id for hit in hits}
    assert malformed_id not in {hit.memory_id for hit in hits}
