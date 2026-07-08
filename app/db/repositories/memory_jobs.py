from __future__ import annotations

from typing import Any

from sqlalchemy.exc import IntegrityError

from app.db.models import MemoryExtractionJobModel, MessageModel
from app.db.repositories.common import (
    SessionFactory,
    dump_json,
    load_json_list,
    load_json_object,
)
from app.memory.schemas import MemoryExtractionJobRecord, MemoryJobEnqueueResult
from app.schemas import MessageRecord
from app.utils.ids import new_id, utc_now


class MemoryJobRepository:
    def __init__(self, session_factory: SessionFactory):
        self._session_factory = session_factory

    def _job_from_model(self, model: MemoryExtractionJobModel) -> MemoryExtractionJobRecord:
        return MemoryExtractionJobRecord(
            id=model.id,
            idempotency_key=model.idempotency_key,
            user_id=model.user_id,
            session_id=model.session_id,
            character_id=model.character_id,
            trigger_type=model.trigger_type,
            status=model.status,
            source_message_ids=[
                str(message_id) for message_id in load_json_list(model.source_message_ids_json)
            ],
            input_payload=load_json_object(model.input_json),
            result_payload=(
                load_json_object(model.result_json) if model.result_json else None
            ),
            error=model.error,
            attempt_count=model.attempt_count,
            locked_by=model.locked_by,
            locked_at=model.locked_at,
            created_at=model.created_at,
            updated_at=model.updated_at,
        )

    def _message_from_model(self, model: MessageModel) -> MessageRecord:
        return MessageRecord(
            id=model.id,
            session_id=model.session_id,
            user_id=model.user_id,
            role=model.role,
            content=model.content,
            created_at=model.created_at,
            emotion_label=model.emotion_label,
            importance_score=model.importance_score,
        )

    def list_unprocessed_window_messages(
        self,
        *,
        session_id: str,
        limit: int,
    ) -> list[MessageRecord]:
        with self._session_factory() as session:
            messages = (
                session.query(MessageModel)
                .filter(MessageModel.session_id == session_id)
                .order_by(MessageModel.created_at.asc())
                .all()
            )
            jobs = (
                session.query(MemoryExtractionJobModel.source_message_ids_json)
                .filter(
                    MemoryExtractionJobModel.session_id == session_id,
                    MemoryExtractionJobModel.trigger_type == "window",
                    MemoryExtractionJobModel.status.in_(
                        ["pending", "running", "completed"]
                    ),
                )
                .all()
            )
        covered_ids: set[str] = set()
        for row in jobs:
            covered_ids.update(str(message_id) for message_id in load_json_list(row[0]))
        return [
            self._message_from_model(message)
            for message in messages
            if message.id not in covered_ids
        ][:limit]

    def enqueue_memory_extraction_job(
        self,
        *,
        idempotency_key: str,
        user_id: str,
        session_id: str,
        character_id: str,
        trigger_type: str,
        source_message_ids: list[str],
        input_payload: dict[str, Any],
    ) -> MemoryJobEnqueueResult:
        job_id = new_id("memjob")
        now = utc_now()
        created = True
        with self._session_factory() as session:
            try:
                session.add(
                    MemoryExtractionJobModel(
                        id=job_id,
                        idempotency_key=idempotency_key,
                        user_id=user_id,
                        session_id=session_id,
                        character_id=character_id,
                        trigger_type=trigger_type,
                        status="pending",
                        source_message_ids_json=dump_json(source_message_ids),
                        input_json=dump_json(input_payload, sort_keys=True),
                        result_json=None,
                        error=None,
                        attempt_count=0,
                        locked_by=None,
                        locked_at=None,
                        created_at=now,
                        updated_at=None,
                    )
                )
                session.commit()
            except IntegrityError:
                session.rollback()
                created = False
            job = (
                session.query(MemoryExtractionJobModel)
                .filter(MemoryExtractionJobModel.idempotency_key == idempotency_key)
                .limit(1)
                .one()
            )
            return MemoryJobEnqueueResult(job=self._job_from_model(job), created=created)

    def claim_next_memory_extraction_job(
        self,
        *,
        worker_id: str,
    ) -> MemoryExtractionJobRecord | None:
        now = utc_now()
        with self._session_factory() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            job = (
                session.query(MemoryExtractionJobModel)
                .filter(MemoryExtractionJobModel.status == "pending")
                .order_by(MemoryExtractionJobModel.created_at.asc())
                .limit(1)
                .one_or_none()
            )
            if job is None:
                session.commit()
                return None
            job.status = "running"
            job.locked_by = worker_id
            job.locked_at = now
            job.attempt_count += 1
            job.updated_at = now
            session.commit()
            return self._job_from_model(job)

    def complete_memory_extraction_job(
        self,
        *,
        job_id: str,
        result_payload: dict[str, Any],
    ) -> None:
        with self._session_factory() as session:
            job = session.get(MemoryExtractionJobModel, job_id)
            if job is not None:
                job.status = "completed"
                job.result_json = dump_json(result_payload, sort_keys=True)
                job.error = None
                job.locked_by = None
                job.locked_at = None
                job.updated_at = utc_now()
            session.commit()

    def fail_memory_extraction_job(self, *, job_id: str, error: str) -> None:
        with self._session_factory() as session:
            job = session.get(MemoryExtractionJobModel, job_id)
            if job is not None:
                job.status = "failed"
                job.error = error
                job.locked_by = None
                job.locked_at = None
                job.updated_at = utc_now()
            session.commit()

    def list_memory_extraction_jobs(
        self,
        *,
        session_id: str | None = None,
    ) -> list[MemoryExtractionJobRecord]:
        with self._session_factory() as session:
            query = session.query(MemoryExtractionJobModel)
            if session_id:
                query = query.filter(MemoryExtractionJobModel.session_id == session_id)
            rows = query.order_by(MemoryExtractionJobModel.created_at.asc()).all()
        return [self._job_from_model(row) for row in rows]
