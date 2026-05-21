from __future__ import annotations

from app.memory.store import MemoryStore


class MemoryAdminService:
    def __init__(self, store: MemoryStore):
        self.store = store

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

    def archive_memory(self, *, memory_id: str) -> str:
        self.store.archive_memory(memory_id=memory_id)
        return f"archived memory: {memory_id}"

    def disable_memory(self, *, memory_id: str) -> str:
        self.store.disable_memory(memory_id=memory_id)
        return f"disabled memory: {memory_id}"

    def handle_command(self, *, command: str, user_id: str) -> str:
        parts = command.strip().split()
        if len(parts) >= 2 and parts[1] == "list":
            return self.list_memories(user_id=user_id)
        if len(parts) == 3 and parts[1] == "delete":
            return self.archive_memory(memory_id=parts[2])
        if len(parts) == 3 and parts[1] == "disable":
            return self.disable_memory(memory_id=parts[2])
        return "invalid memory command. Use /memory list|delete|disable"

