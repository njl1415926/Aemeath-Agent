from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def _parse_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    deepseek_api_key: str | None
    deepseek_base_url: str
    aemeath_model: str
    deepseek_thinking: str
    deepseek_reasoning_effort: str
    db_path: Path
    recent_message_limit: int
    use_mock_llm: bool
    character_card_path: Path
    prompt_template_path: Path
    default_user_id: str = "user_default"
    default_character_id: str = "aemeath"

    @staticmethod
    def from_env() -> "Settings":
        load_dotenv()

        db_path = Path(os.getenv("AEMEATH_DB_PATH", "data/aemeath.db"))
        recent_limit = int(os.getenv("AEMEATH_RECENT_MESSAGE_LIMIT", "12"))
        use_mock = _parse_bool(os.getenv("AEMEATH_USE_MOCK_LLM", "true"))

        return Settings(
            deepseek_api_key=os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY"),
            deepseek_base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            aemeath_model=os.getenv(
                "DEEPSEEK_MODEL",
                os.getenv("AEMEATH_MODEL", "deepseek-v4-pro"),
            ),
            deepseek_thinking=os.getenv("DEEPSEEK_THINKING", "disabled"),
            deepseek_reasoning_effort=os.getenv("DEEPSEEK_REASONING_EFFORT", ""),
            db_path=db_path,
            recent_message_limit=max(1, recent_limit),
            use_mock_llm=use_mock,
            character_card_path=Path(
                os.getenv("AEMEATH_CHARACTER_CARD_PATH", "app/character/aemeath.v0.1.yaml")
            ),
            prompt_template_path=Path(
                os.getenv("AEMEATH_DIALOGUE_PROMPT_PATH", "app/prompts/dialogue_prompt.v0.1.md")
            ),
        )

    def validate(self) -> None:
        if self.use_mock_llm:
            return
        if not self.deepseek_api_key:
            raise ValueError(
                "DEEPSEEK_API_KEY is required when AEMEATH_USE_MOCK_LLM=false."
            )
