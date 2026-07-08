from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

from app.db.sqlalchemy_store import SqlAlchemyStore
from app.memory.extractor import MemoryExtractionService
from app.memory.mock_extractor import MockMemoryExtractionModelClient
from app.memory.worker import _messages_for_job
from app.retrieval.embeddings import DeterministicHashEmbeddingProvider


def test_sqlalchemy_enabled_worker_single_job_smoke(tmp_path: Path):
    store = SqlAlchemyStore(tmp_path / "worker_smoke.db")
    session = store.create_session(user_id="user_1", character_id="aemeath")
    message_id = store.save_message(
        session_id=session.id,
        user_id="user_1",
        role="user",
        content="记住，我喜欢红茶。",
    )
    queued = store.enqueue_memory_extraction_job(
        idempotency_key="worker_smoke_job",
        user_id="user_1",
        session_id=session.id,
        character_id="aemeath",
        trigger_type="hard",
        source_message_ids=[message_id],
        input_payload={"messages": []},
    )
    embedder = DeterministicHashEmbeddingProvider()
    extraction_service = MemoryExtractionService(
        store=store,
        model_client=MockMemoryExtractionModelClient(),
        embedding_provider=embedder,
    )

    job = store.claim_next_memory_extraction_job(worker_id="worker_sqlalchemy")
    assert job is not None
    assert job.id == queued.job.id

    result = extraction_service.extract_and_persist(
        user_id=job.user_id,
        session_id=job.session_id,
        character_id=job.character_id,
        messages=_messages_for_job(store, job),
        trigger_type=job.trigger_type,
        source_job_id=job.id,
    )
    store.complete_memory_extraction_job(
        job_id=job.id,
        result_payload={
            "created_memory_ids": result.created_memory_ids,
            "skipped_contents": result.skipped_contents,
        },
    )

    [completed] = store.list_memory_extraction_jobs(session_id=session.id)
    assert completed.status == "completed"
    assert completed.result_payload == {
        "created_memory_ids": result.created_memory_ids,
        "skipped_contents": [],
    }
    assert len(result.created_memory_ids) == 1

    memory = store.get_memory(memory_id=result.created_memory_ids[0])
    assert memory is not None
    assert memory.content == "我喜欢红茶"
    assert memory.source_session_id == session.id
    assert memory.source_job_id == job.id
    assert memory.source_message_ids == [message_id]

    hits = store.search_memory_embeddings(
        user_id="user_1",
        character_id="aemeath",
        session_id=session.id,
        query_embedding=embedder.embed_query("红茶"),
        limit=4,
    )
    assert [hit.memory_id for hit in hits] == [memory.id]


def test_sqlalchemy_two_store_instances_claim_one_pending_job_once(tmp_path: Path):
    db_path = tmp_path / "claim.db"
    creator = SqlAlchemyStore(db_path)
    session = creator.create_session(user_id="user_1", character_id="aemeath")
    creator.enqueue_memory_extraction_job(
        idempotency_key="claim_once_job",
        user_id="user_1",
        session_id=session.id,
        character_id="aemeath",
        trigger_type="hard",
        source_message_ids=[],
        input_payload={},
    )
    first_store = SqlAlchemyStore(db_path)
    second_store = SqlAlchemyStore(db_path)
    barrier = Barrier(2)

    def claim(store: SqlAlchemyStore, worker_id: str):
        barrier.wait()
        return store.claim_next_memory_extraction_job(worker_id=worker_id)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda args: claim(*args),
                [(first_store, "worker_1"), (second_store, "worker_2")],
            )
        )

    claimed = [job for job in results if job is not None]
    assert len(claimed) == 1
    assert claimed[0].status == "running"
    assert claimed[0].attempt_count == 1
    assert claimed[0].locked_by in {"worker_1", "worker_2"}

    [stored] = creator.list_memory_extraction_jobs(session_id=session.id)
    assert stored.status == "running"
    assert stored.attempt_count == 1
    assert stored.locked_by == claimed[0].locked_by
