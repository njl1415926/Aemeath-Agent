from __future__ import annotations

from app.schemas import AgentTurnInput
from app.utils.ids import new_id


def test_memory_write_for_explicit_instruction_enqueues_job(long_term_memory_controller_and_store):
    controller, store, settings = long_term_memory_controller_and_store
    session_id = new_id("session")
    turn = AgentTurnInput(
        user_id=settings.default_user_id,
        session_id=session_id,
        character_id=settings.default_character_id,
        message="记住，我这个项目想重点研究记忆机制。",
        metadata={},
    )
    controller.handle_user_message(turn)
    jobs = store.list_memory_extraction_jobs(session_id=session_id)
    assert len(jobs) == 1
    assert jobs[0].trigger_type == "hard"
    assert jobs[0].status == "pending"
    memories = store.list_active_memories(user_id=settings.default_user_id)
    assert memories == []


def test_memory_not_written_for_small_talk(long_term_memory_controller_and_store):
    controller, store, settings = long_term_memory_controller_and_store
    session_id = new_id("session")
    turn = AgentTurnInput(
        user_id=settings.default_user_id,
        session_id=session_id,
        character_id=settings.default_character_id,
        message="今天随便聊两句。",
        metadata={},
    )
    controller.handle_user_message(turn)
    jobs = store.list_memory_extraction_jobs(session_id=session_id)
    assert jobs == []
    memories = store.list_active_memories(user_id=settings.default_user_id)
    assert memories == []
