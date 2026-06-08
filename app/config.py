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
    enable_long_term_memory: bool = False
    enable_prompt_run_log: bool = True
    prompt_run_log_root: Path = Path(".aemeath/sessions")
    memory_embedding_provider: str = "hash"
    memory_embedding_model_path: Path = Path(".cache/huggingface/BAAI/bge-m3")
    memory_vector_db_path: Path = Path("data/aemeath_memory.sqlite")
    memory_recall_top_k: int = 4
    memory_recall_threshold: float = 0.45
    memory_worker_poll_interval_seconds: float = 2.0
    memory_window_size: int = 12
    enable_memory_window_extraction: bool = True
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
                os.getenv("AEMEATH_CHARACTER_CARD_PATH", "app/character/Aemeath.v0.2")
            ),
            prompt_template_path=Path(
                os.getenv("AEMEATH_DIALOGUE_PROMPT_PATH", "app/prompts/dialogue_prompt.v0.1.md")
            ),
            enable_long_term_memory=_parse_bool(
                os.getenv("AEMEATH_ENABLE_LONG_TERM_MEMORY", "false")
            ),
            enable_prompt_run_log=_parse_bool(
                os.getenv("AEMEATH_ENABLE_PROMPT_RUN_LOG", "true")
            ),
            prompt_run_log_root=Path(
                os.getenv("AEMEATH_PROMPT_RUN_LOG_ROOT", ".aemeath/sessions")
            ),
            memory_embedding_provider=os.getenv(
                "AEMEATH_MEMORY_EMBEDDING_PROVIDER",
                "hash",
            ),
            memory_embedding_model_path=Path(
                os.getenv(
                    "AEMEATH_MEMORY_EMBEDDING_MODEL_PATH",
                    ".cache/huggingface/BAAI/bge-m3",
                )
            ),
            memory_vector_db_path=Path(
                os.getenv("AEMEATH_MEMORY_VECTOR_DB_PATH", "data/aemeath_memory.sqlite")
            ),
            memory_recall_top_k=max(
                1,
                int(os.getenv("AEMEATH_MEMORY_RECALL_TOP_K", "4")),
            ),
            memory_recall_threshold=float(
                os.getenv("AEMEATH_MEMORY_RECALL_THRESHOLD", "0.45")
            ),
            memory_worker_poll_interval_seconds=max(
                0.2,
                float(os.getenv("AEMEATH_MEMORY_WORKER_POLL_INTERVAL_SECONDS", "2")),
            ),
            memory_window_size=max(1, int(os.getenv("AEMEATH_MEMORY_WINDOW_SIZE", "12"))),
            enable_memory_window_extraction=_parse_bool(
                os.getenv("AEMEATH_ENABLE_MEMORY_WINDOW_EXTRACTION", "true")
            ),
        )

    def validate(self) -> None:
        if self.use_mock_llm:
            return
        if not self.deepseek_api_key:
            raise ValueError(
                "DEEPSEEK_API_KEY is required when AEMEATH_USE_MOCK_LLM=false."
            )
