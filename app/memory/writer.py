from __future__ import annotations

from app.memory.jobs import build_memory_job_idempotency_key
from app.memory.store import MemoryStore
from app.schemas import MemoryDecision


class MemoryWriter:
    def __init__(self, store: MemoryStore):
        self.store = store

    def persist_decision(
        self,
        *,
        user_id: str,
        decision: MemoryDecision,
        source_message_id: str | None = None,
        session_id: str,
        character_id: str,
    ) -> str | None:
        if not decision.should_write:
            return None
        if decision.operation not in {"create", "update", "merge"}:
            return None
        if not decision.content or not decision.namespace or not decision.memory_type:
            return None
        if not session_id or not character_id:
            raise ValueError("memory write requires verified session_id and character_id")
        source_message_ids = list(dict.fromkeys(decision.source_message_ids))
        if source_message_id:
            source_message_ids.insert(0, source_message_id)
            source_message_ids = list(dict.fromkeys(source_message_ids))
        for message_id in source_message_ids:
            message = self.store.get_message(message_id=message_id)
            if (
                message is None
                or message.user_id != user_id
                or message.session_id != session_id
            ):
                raise ValueError("memory source message does not match the current context")

        if self.store.has_exact_active_memory(
            user_id=user_id,
            scope="user_global",
            scope_character_id=None,
            scope_session_id=None,
            content=decision.content,
        ):
            return None

        memory_id = self.store.create_memory(
            user_id=user_id,
            scope="user_global",
            scope_character_id=None,
            scope_session_id=None,
            namespace=decision.namespace,
            memory_type=decision.memory_type,
            content=decision.content,
            importance=decision.importance or 5,
            confidence=decision.confidence or 0.8,
            source=decision.source or "inferred_from_dialogue",
            source_message_id=source_message_id,
            source_session_id=session_id,
            source_job_id=None,
            source_message_ids=source_message_ids,
            related_memory_ids=decision.related_memory_ids,
            expiry=decision.expiry,
        )
        return memory_id

    def enqueue_window_if_due(
        self,
        *,
        user_id: str,
        session_id: str,
        character_id: str,
    ) -> str | None:
        return None


class QueuedMemoryWriter:
    def __init__(
        self,
        store: MemoryStore,
        *,
        window_size: int = 12,
        enable_window_extraction: bool = True,
    ):
        self.store = store
        self.window_size = window_size
        self.enable_window_extraction = enable_window_extraction

    def persist_decision(
        self,
        *,
        user_id: str,
        decision: MemoryDecision,
        source_message_id: str | None = None,
        session_id: str,
        character_id: str,
    ) -> str | None:
        if not decision.should_write:
            return None
        if not session_id or not character_id:
            raise ValueError("memory write requires verified session_id and character_id")
        if not source_message_id:
            return None
        message = self.store.get_message(message_id=source_message_id)
        if (
            message is None
            or message.user_id != user_id
            or message.session_id != session_id
        ):
            raise ValueError("memory source message does not match the current context")
        idempotency_key = build_memory_job_idempotency_key(
            user_id=user_id,
            character_id=character_id,
            session_id=session_id,
            trigger_type="hard",
            source_message_ids=[source_message_id],
        )
        result = self.store.enqueue_memory_extraction_job(
            idempotency_key=idempotency_key,
            user_id=user_id,
            session_id=session_id,
            character_id=character_id,
            trigger_type="hard",
            source_message_ids=[source_message_id],
            input_payload={
                "messages": [
                    {
                        "id": message.id,
                        "role": message.role,
                        "content": message.content,
                        "created_at": message.created_at,
                    }
                ],
                "decision_reason": decision.reason,
            },
        )
        return result.job.id

    def enqueue_window_if_due(
        self,
        *,
        user_id: str,
        session_id: str,
        character_id: str,
    ) -> str | None:
        if not self.enable_window_extraction:
            return None
        if not session_id or not character_id:
            raise ValueError("memory write requires verified session_id and character_id")
        messages = self.store.list_unprocessed_window_messages(
            session_id=session_id,
            limit=self.window_size,
        )
        if len(messages) < self.window_size:
            return None
        if any(
            message.user_id != user_id or message.session_id != session_id
            for message in messages
        ):
            raise ValueError("memory window messages do not match the current context")
        source_message_ids = [message.id for message in messages]
        idempotency_key = build_memory_job_idempotency_key(
            user_id=user_id,
            character_id=character_id,
            session_id=session_id,
            trigger_type="window",
            source_message_ids=source_message_ids,
        )
        result = self.store.enqueue_memory_extraction_job(
            idempotency_key=idempotency_key,
            user_id=user_id,
            session_id=session_id,
            character_id=character_id,
            trigger_type="window",
            source_message_ids=source_message_ids,
            input_payload={
                "messages": [
                    {
                        "id": message.id,
                        "role": message.role,
                        "content": message.content,
                        "created_at": message.created_at,
                    }
                    for message in messages
                ]
            },
        )
        return result.job.id
