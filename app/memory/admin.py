from __future__ import annotations

from app.memory.extractor import MemoryExtractionService
from app.memory.store import MemoryStore
from app.session.ports import SessionStorePort


class MemoryAdminService:
    def __init__(
        self,
        store: MemoryStore,
        *,
        session_store: SessionStorePort,
        extraction_service: MemoryExtractionService | None = None,
    ):
        self.store = store
        self.session_store = session_store
        self.extraction_service = extraction_service

    def list_memories(self, *, user_id: str) -> str:
        memories = self.store.list_active_memories(user_id=user_id, limit=100)
        if not memories:
            return "(no active memories)"
        lines = []
        for memory in memories:
            lines.append(
                f"{memory.id} | {memory.memory_type} | "
                f"importance={memory.importance} | {memory.content}"
            )
        return "\n".join(lines)

    def archive_memory(self, *, memory_id: str, user_id: str) -> str:
        updated = self.store.archive_memory(memory_id=memory_id, user_id=user_id)
        if not updated:
            return "memory not found or unavailable"
        return f"archived memory: {memory_id}"

    def disable_memory(self, *, memory_id: str, user_id: str) -> str:
        updated = self.store.disable_memory(memory_id=memory_id, user_id=user_id)
        if not updated:
            return "memory not found or unavailable"
        return f"disabled memory: {memory_id}"

    def extract_recent_session_memories(
        self,
        *,
        user_id: str,
        session_id: str,
        character_id: str,
        limit: int = 6,
    ) -> str:
        if self.extraction_service is None:
            return "long-term memory extraction is disabled"
        self.session_store.require_session(
            session_id=session_id,
            user_id=user_id,
            character_id=character_id,
        )
        messages = self.store.get_recent_messages(session_id=session_id, limit=limit)
        if not messages:
            return "[memory extract]\nno recent messages"
        result = self.extraction_service.extract_and_persist(
            user_id=user_id,
            session_id=session_id,
            character_id=character_id,
            messages=messages,
            trigger_type="manual",
        )
        lines = ["[memory extract]"]
        if not result.created_memory_ids and not result.skipped_contents:
            lines.append("no long-term memories extracted")
            return "\n".join(lines)
        for memory_id in result.created_memory_ids:
            memory = self.store.get_memory(memory_id=memory_id)
            lines.append(f"created {memory_id}")
            lines.append(f"- {memory.content if memory else '(missing memory record)'}")
        for content in result.skipped_contents:
            lines.append("skipped duplicate")
            lines.append(f"- {content}")
        return "\n".join(lines)

    def handle_command(
        self,
        *,
        command: str,
        user_id: str,
        session_id: str | None = None,
        character_id: str | None = None,
    ) -> str:
        parts = command.strip().split()
        if len(parts) >= 2 and parts[1] == "list":
            return self.list_memories(user_id=user_id)
        if len(parts) == 3 and parts[1] == "delete":
            return self.archive_memory(memory_id=parts[2], user_id=user_id)
        if len(parts) == 3 and parts[1] == "disable":
            return self.disable_memory(memory_id=parts[2], user_id=user_id)
        if len(parts) >= 2 and parts[1] == "extract":
            if not session_id:
                return "memory extract requires an active session"
            if not character_id:
                return "memory extract requires an active character"
            limit = 6
            if len(parts) == 3:
                try:
                    limit = max(1, int(parts[2]))
                except ValueError:
                    return "usage: /memory extract [limit]"
            return self.extract_recent_session_memories(
                user_id=user_id,
                session_id=session_id,
                character_id=character_id,
                limit=limit,
            )
        return "invalid memory command. Use /memory list|delete|disable|extract [limit]"
