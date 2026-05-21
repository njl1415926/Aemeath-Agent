from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings
from app.main import build_controller


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings(
        deepseek_api_key=None,
        deepseek_base_url="https://api.deepseek.com",
        aemeath_model="deepseek-v4-pro",
        deepseek_thinking="disabled",
        deepseek_reasoning_effort="high",
        db_path=tmp_path / "aemeath_test.db",
        recent_message_limit=12,
        use_mock_llm=True,
        character_card_path=Path("app/character/aemeath.v0.1.yaml"),
        prompt_template_path=Path("app/prompts/dialogue_prompt.v0.1.md"),
    )


@pytest.fixture()
def controller_and_store(settings: Settings):
    controller, _memory_admin, resolved_settings, store = build_controller(settings=settings)
    return controller, store, resolved_settings
