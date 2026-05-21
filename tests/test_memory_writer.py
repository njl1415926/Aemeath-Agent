from __future__ import annotations

from app.schemas import AgentTurnInput
from app.utils.ids import new_id


def test_memory_write_for_explicit_instruction(controller_and_store):
    controller, store, settings = controller_and_store
    session_id = new_id("session")
    turn = AgentTurnInput(
        user_id=settings.default_user_id,
        session_id=session_id,
        character_id=settings.default_character_id,
        message="记住，我这个项目想重点研究记忆机制。",
        metadata={},
    )
    controller.handle_user_message(turn)
    memories = store.list_active_memories(user_id=settings.default_user_id)
    assert memories
    assert any("记忆机制" in memory.content for memory in memories)


def test_memory_not_written_for_small_talk(controller_and_store):
    controller, store, settings = controller_and_store
    session_id = new_id("session")
    turn = AgentTurnInput(
        user_id=settings.default_user_id,
        session_id=session_id,
        character_id=settings.default_character_id,
        message="今天随便聊两句。",
        metadata={},
    )
    controller.handle_user_message(turn)
    memories = store.list_active_memories(user_id=settings.default_user_id)
    assert memories == []

