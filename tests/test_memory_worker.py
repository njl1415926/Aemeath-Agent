from __future__ import annotations

from pathlib import Path

from app.db.sqlite_store import SQLiteStore
from app.memory.worker import _process_memory_extraction_job


class FailingExtractionService:
    def extract_and_persist(self, **_kwargs):
        raise ValueError(
            "provider failed token=secret-token api-key=secret-api-key "
            "Authorization: Bearer secret-bearer cookie=secret-cookie"
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
