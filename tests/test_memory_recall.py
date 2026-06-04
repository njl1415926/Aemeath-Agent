from __future__ import annotations

from app.schemas import AgentTurnInput
from app.utils.ids import new_id


def test_memory_recall_uses_previous_written_memory(long_term_memory_controller_and_store):
    controller, store, settings = long_term_memory_controller_and_store
    session_id = new_id("session")

    controller.handle_user_message(
        AgentTurnInput(
            user_id=settings.default_user_id,
            session_id=session_id,
            character_id=settings.default_character_id,
            message="记住，我这个项目最想研究记忆机制。",
            metadata={},
        )
    )

    output = controller.handle_user_message(
        AgentTurnInput(
            user_id=settings.default_user_id,
            session_id=session_id,
            character_id=settings.default_character_id,
            message="你还记得我这个项目最想研究什么吗？",
            metadata={},
        )
    )
    assert "记忆机制" in output.response
    assert output.used_memory_ids

    memories = store.list_active_memories(user_id=settings.default_user_id)
    assert max(memory.use_count for memory in memories) >= 1
