from __future__ import annotations

from app.schemas import AgentTurnInput
from app.utils.ids import new_id


def test_state_updates_for_tired_input(controller_and_store):
    controller, store, settings = controller_and_store
    session_id = new_id("session")
    controller.handle_user_message(
        AgentTurnInput(
            user_id=settings.default_user_id,
            session_id=session_id,
            character_id=settings.default_character_id,
            message="今天好累，不想写代码。",
            metadata={},
        )
    )
    state = store.get_or_create_relationship_state(
        user_id=settings.default_user_id,
        character_id=settings.default_character_id,
    )
    assert state.mood == "concerned"
    assert state.user_emotion == "tired"
    assert state.affection == 51
