from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from app.db.sqlite_store import SQLiteStore
from app.memory import worker
from app.memory.schemas import MemoryExtractionResult
from app.memory.worker import _process_memory_extraction_job


class FailingExtractionService:
    def extract_and_persist(self, **_kwargs):
        raise ValueError(
            "provider failed token=secret-token api-key=secret-api-key "
            "Authorization: Bearer secret-bearer cookie=secret-cookie"
        )


class PendingArtifactExtractionService:
    def extract_and_persist(self, **_kwargs):
        return MemoryExtractionResult(
            created_memory_ids=["memory_created"],
            existing_memory_ids=["memory_existing"],
            artifact_written_memory_ids=["memory_created"],
            embedding_pending_memory_ids=["memory_existing"],
            extracted_memories=[],
        )


def test_memory_worker_failure_error_is_sanitized(tmp_path: Path, capsys):
    store = SQLiteStore(tmp_path / "worker_failure.db")
    session = store.create_session(user_id="user_1", character_id="aemeath")
    job = store.enqueue_memory_extraction_job(
        idempotency_key="failure_job",
        user_id="user_1",
        session_id=session.id,
        character_id="aemeath",
        trigger_type="hard",
        source_message_ids=[],
        input_payload={},
    ).job

    _process_memory_extraction_job(
        store=store,
        extraction_service=FailingExtractionService(),  # type: ignore[arg-type]
        job=job,
    )

    [failed_job] = store.list_memory_extraction_jobs(session_id=session.id)
    captured = capsys.readouterr().out
    assert failed_job.status == "failed"
    assert "secret-token" not in failed_job.error
    assert "secret-api-key" not in failed_job.error
    assert "secret-bearer" not in failed_job.error
    assert "secret-cookie" not in failed_job.error
    assert "secret-token" not in captured
    assert "secret-api-key" not in captured
    assert "secret-bearer" not in captured
    assert "secret-cookie" not in captured
    assert "token=[redacted]" in failed_job.error


def test_memory_worker_completes_fact_job_with_fixed_artifact_result_fields(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "worker_pending.db")
    session = store.create_session(user_id="user_1", character_id="aemeath")
    job = store.enqueue_memory_extraction_job(
        idempotency_key="pending_artifact_job",
        user_id="user_1",
        session_id=session.id,
        character_id="aemeath",
        trigger_type="hard",
        source_message_ids=[],
        input_payload={},
    ).job

    _process_memory_extraction_job(
        store=store,
        extraction_service=PendingArtifactExtractionService(),  # type: ignore[arg-type]
        job=job,
    )

    [completed_job] = store.list_memory_extraction_jobs(session_id=session.id)
    assert completed_job.status == "completed"
    assert completed_job.result_payload == {
        "created_memory_ids": ["memory_created"],
        "existing_memory_ids": ["memory_existing"],
        "artifact_written_memory_ids": ["memory_created"],
        "embedding_pending_memory_ids": ["memory_existing"],
    }


def test_memory_worker_closes_registered_resources_when_active_loop_fails(
    settings,
    monkeypatch,
) -> None:
    closed = []

    def fail(_settings, *, app_logger, resources):
        resources.register(name="test_resource", close=lambda: closed.append(True))
        raise RuntimeError("worker failure")

    monkeypatch.setattr(worker, "_run_worker_active", fail)
    with pytest.raises(RuntimeError):
        worker.run_worker(replace(settings, enable_long_term_memory=True))
    assert closed == [True]
