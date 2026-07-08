from __future__ import annotations

import time

from app.config import Settings
from app.db.store_factory import StoreFacade, create_store
from app.memory.extractor import MemoryExtractionService
from app.memory.mock_extractor import MockMemoryExtractionModelClient
from app.memory.runtime import build_memory_embedding_provider
from app.model.deepseek import DeepSeekClient, FallbackChatModelClient
from app.model.mock import MockChatModelClient
from app.schemas import MessageRecord
from app.utils.ids import new_id


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


def run_worker(settings: Settings | None = None) -> int:
    settings = settings or Settings.from_env()
    settings.validate()
    if not settings.enable_long_term_memory:
        print("[memory worker] long-term memory is disabled")
        return 0

    store = create_store(db_path=settings.db_path, backend=settings.db_backend)
    embedding_provider = build_memory_embedding_provider(settings)
    print(
        "[memory worker] loading embedding provider "
        f"{embedding_provider.model_name}"
    )
    embedding_provider.embed_query("记忆系统预热")
    extraction_service = MemoryExtractionService(
        store=store,
        model_client=(
            MockMemoryExtractionModelClient()
            if settings.use_mock_llm
            else _build_model_client(settings)
        ),
        embedding_provider=embedding_provider,
    )
    worker_id = new_id("memworker")
    print(
        f"[memory worker] worker_id={worker_id} "
        f"db={settings.db_path} poll={settings.memory_worker_poll_interval_seconds}s"
    )
    try:
        while True:
            job = store.claim_next_memory_extraction_job(worker_id=worker_id)
            if job is None:
                time.sleep(settings.memory_worker_poll_interval_seconds)
                continue
            print(f"[memory worker] running {job.id} trigger={job.trigger_type}")
            try:
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
                print(
                    f"[memory worker] completed {job.id} "
                    f"created={len(result.created_memory_ids)}"
                )
            except Exception as exc:  # noqa: BLE001
                store.fail_memory_extraction_job(job_id=job.id, error=str(exc))
                print(f"[memory worker] failed {job.id}: {exc}")
    except KeyboardInterrupt:
        print("[memory worker] bye")
        return 0


if __name__ == "__main__":
    raise SystemExit(run_worker())
