from __future__ import annotations

from pathlib import Path

from app.character.provider import CharacterRegistry
from app.schemas import AgentTurnInput
from app.utils.ids import new_id


def test_character_registry_loads_default_character_by_id():
    registry = CharacterRegistry.from_single_card(
        character_card_path=Path("app/character/Aemeath.v0.2"),
        default_character_id="aemeath",
    )

    profile = registry.load_profile(character_id="aemeath")
    context = registry.build_runtime_context(
        character_id="aemeath",
    )

    assert profile.character_id == "aemeath"
    assert context.character_id == "aemeath"


def test_controller_falls_back_when_character_id_is_unknown(controller_and_store):
    controller, _store, settings = controller_and_store

    output = controller.handle_user_message(
        AgentTurnInput(
            user_id=settings.default_user_id,
            session_id=new_id("session"),
            character_id="unknown_character",
            message="hello",
            metadata={},
        )
    )

    assert "character_id_unknown_using_default_profile" in output.warnings
