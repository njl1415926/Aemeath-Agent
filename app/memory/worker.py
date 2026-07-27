from __future__ import annotations

import time
from dataclasses import dataclass

from app.config import Settings
from app.db.store_factory import StoreFacade, create_store
from app.errors import map_exception
from app.memory.extractor import MemoryExtractionService
from app.memory.mock_extractor import MockMemoryExtractionModelClient
from app.memory.observability import record_memory_event
from app.memory.repair import MemoryEmbeddingRepairService
from app.memory.runtime import build_memory_embedding_provider
from app.memory.schemas import (
    MemoryEmbeddingArtifactGapCursor,
    MemoryEmbeddingRepairResult,
)
from app.model.deepseek import DeepSeekClient, FallbackChatModelClient
from app.model.mock import MockChatModelClient
from app.observability.noop import NoopAppLogger
from app.observability.ports import AppLoggerPort
from app.observability.runtime import create_app_logger
from app.schemas import MessageRecord
from app.utils.ids import new_id


@dataclass
class MemoryRepairSchedule:
    interval_seconds: float
    max_backoff_seconds: float
    cursor: MemoryEmbeddingArtifactGapCursor | None = None
    next_run_at: float = 0.0
    current_backoff_seconds: float | None = None

    def due(self, now: float) -> bool:
        return now >= self.next_run_at

    def record_result(self, result: MemoryEmbeddingRepairResult, *, now: float) -> None:
        self.cursor = result.next_cursor
        if result.embedding_pending_memory_ids:
            self.record_failure(now=now)
            return
        self.current_backoff_seconds = self.interval_seconds
        self.next_run_at = now + self.interval_seconds

    def record_failure(self, *, now: float) -> float:
        delay = self.current_backoff_seconds or self.interval_seconds
        self.next_run_at = now + delay
        self.current_backoff_seconds = min(self.max_backoff_seconds, delay * 2.0)
        return delay


def _build_model_client(settings: Settings):
    if settings.use_mock_llm:
        return MockChatModelClient()
    deepseek_client = DeepSeekClient(
        api_key=settings.deepseek_api_key or "",
        base_url=settings.deepseek_base_url,
        model=settings.aemeath_model,
        thinking=settings.deepseek_thinking,
        reasoning_effort=settings.deepseek_reasoning_effort,
    )
    return FallbackChatModelClient(primary=deepseek_client, fallback=MockChatModelClient())


def _messages_for_job(store: StoreFacade, job) -> list[MessageRecord]:
    messages: list[MessageRecord] = []
    for message_id in job.source_message_ids:
        message = store.get_message(message_id=message_id)
        if message is not None:
            messages.append(message)
    if messages:
        return messages
    payload_messages = job.input_payload.get("messages", [])
    for item in payload_messages:
        messages.append(
            MessageRecord(
                id=str(item.get("id", "")),
                session_id=job.session_id,
                user_id=job.user_id,
                role=str(item.get("role", "user")),
                content=str(item.get("content", "")),
                created_at=str(item.get("created_at", "")),
                emotion_label=None,
                importance_score=None,
            )
        )
    return [message for message in messages if message.id and message.content]


def _safe_worker_error(exc: Exception) -> str:
    error_info = map_exception(exc)
    log_payload = error_info.to_log_dict()
    return f"{error_info.code.value}: {log_payload['internal_message']}"


def _process_memory_extraction_job(
    *,
    store: StoreFacade,
    extraction_service: MemoryExtractionService,
    job,
    app_logger: AppLoggerPort | None = None,
) -> None:
    app_logger = app_logger or NoopAppLogger()
    try:
        result = extraction_service.extract_and_persist(
            user_id=job.user_id,
            session_id=job.session_id,
            character_id=job.character_id,
            messages=_messages_for_job(store, job),
            trigger_type=job.trigger_type,
            source_job_id=job.id,
        )
        result_payload = {
            "created_memory_ids": result.created_memory_ids,
            "existing_memory_ids": result.existing_memory_ids,
            "artifact_written_memory_ids": result.artifact_written_memory_ids,
            "embedding_pending_memory_ids": result.embedding_pending_memory_ids,
        }
        store.complete_memory_extraction_job(
            job_id=job.id,
            result_payload=result_payload,
        )
        record_memory_event(
            app_logger,
            event_name="memory.worker_job_completed",
            level="warning" if result.embedding_pending_memory_ids else "info",
            payload={
                "trigger_type": job.trigger_type,
                "created_memory_count": len(result.created_memory_ids),
                "existing_memory_count": len(result.existing_memory_ids),
                "artifact_written_count": len(result.artifact_written_memory_ids),
                "embedding_pending_count": len(result.embedding_pending_memory_ids),
            },
            user_id=job.user_id,
            session_id=job.session_id,
            character_id=job.character_id,
        )
        print(
            f"[memory worker] completed {job.id} "
            f"created={len(result.created_memory_ids)} "
            f"pending={len(result.embedding_pending_memory_ids)}"
        )
    except Exception as exc:  # noqa: BLE001
        safe_error = _safe_worker_error(exc)
        error_info = map_exception(exc)
        store.fail_memory_extraction_job(job_id=job.id, error=safe_error)
        record_memory_event(
            app_logger,
            event_name="memory.worker_job_failed",
            level="error",
            error_code=error_info.code.value,
            payload={"trigger_type": job.trigger_type},
            user_id=job.user_id,
            session_id=job.session_id,
            character_id=job.character_id,
        )
        print(f"[memory worker] failed {job.id}: {safe_error}")


def run_worker(settings: Settings | None = None) -> int:
    settings = settings or Settings.from_env()
    settings.validate()
    app_logger = create_app_logger(
        enabled=settings.enable_app_log,
        root_dir=settings.app_log_root,
    )
    if not settings.enable_long_term_memory:
        print("[memory worker] long-term memory is disabled")
        return 0

    store = create_store(db_path=settings.db_path, backend=settings.db_backend)
    embedding_provider = build_memory_embedding_provider(
        settings,
        app_logger=app_logger,
    )
    extraction_service = MemoryExtractionService(
        store=store,
        model_client=(
            MockMemoryExtractionModelClient()
            if settings.use_mock_llm
            else _build_model_client(settings)
        ),
        embedding_provider=embedding_provider,
        app_logger=app_logger,
    )
    repair_service = MemoryEmbeddingRepairService(
        store=store,
        embedding_provider=embedding_provider,
        app_logger=app_logger,
    )
    repair_schedule = MemoryRepairSchedule(
        interval_seconds=settings.memory_repair_interval_seconds,
        max_backoff_seconds=settings.memory_repair_max_backoff_seconds,
    )
    worker_id = new_id("memworker")
    print(
        f"[memory worker] worker_id={worker_id} "
        f"poll={settings.memory_worker_poll_interval_seconds}s "
        f"repair={settings.memory_repair_interval_seconds}s"
    )
    try:
        while True:
            now = time.monotonic()
            if repair_schedule.due(now):
                try:
                    repair_result = repair_service.repair_batch(
                        cursor=repair_schedule.cursor,
                        limit=settings.memory_repair_batch_size,
                    )
                    repair_schedule.record_result(repair_result, now=now)
                except Exception as exc:  # noqa: BLE001
                    delay = repair_schedule.record_failure(now=now)
                    error_info = map_exception(exc)
                    record_memory_event(
                        app_logger,
                        event_name="memory.repair_failed",
                        level="error",
                        error_code=error_info.code.value,
                        payload={
                            "provider": embedding_provider.signature.provider,
                            "profile_id": embedding_provider.signature.profile_id,
                            "repair_delay_seconds": delay,
                        },
                    )

            job = store.claim_next_memory_extraction_job(worker_id=worker_id)
            if job is not None:
                print(f"[memory worker] running {job.id} trigger={job.trigger_type}")
                _process_memory_extraction_job(
                    store=store,
                    extraction_service=extraction_service,
                    job=job,
                    app_logger=app_logger,
                )
                continue

            until_repair = max(0.05, repair_schedule.next_run_at - time.monotonic())
            time.sleep(min(settings.memory_worker_poll_interval_seconds, until_repair))
    except KeyboardInterrupt:
        print("[memory worker] bye")
        return 0


if __name__ == "__main__":
    raise SystemExit(run_worker())
