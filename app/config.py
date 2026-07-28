from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def _parse_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_embedding_int(name: str, default: str) -> int:
    from app.embeddings.errors import EmbeddingConfigurationError

    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError) as exc:
        raise EmbeddingConfigurationError("embedding_integer_environment_invalid") from exc


def _parse_embedding_float(name: str, default: str) -> float:
    from app.embeddings.errors import EmbeddingConfigurationError

    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError) as exc:
        raise EmbeddingConfigurationError("embedding_float_environment_invalid") from exc


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
    db_backend: str = "sqlite_raw"
    enable_long_term_memory: bool = False
    enable_prompt_run_log: bool = True
    prompt_run_log_root: Path = Path(".aemeath/sessions")
    enable_trace_log: bool = False
    trace_log_root: Path = Path(".aemeath/traces")
    enable_app_log: bool = False
    app_log_root: Path = Path(".aemeath/app_logs")
    memory_embedding_provider: str | None = None
    memory_embedding_model_path: Path = Path(".cache/huggingface/BAAI/bge-m3")
    memory_vector_db_path: Path = Path("data/aemeath_memory.sqlite")
    memory_recall_top_k: int = 4
    memory_recall_threshold: float = 0.45
    memory_worker_poll_interval_seconds: float = 2.0
    memory_repair_batch_size: int = 32
    memory_repair_interval_seconds: float = 30.0
    memory_repair_max_backoff_seconds: float = 300.0
    memory_window_size: int = 12
    enable_memory_window_extraction: bool = True
    default_user_id: str = "user_default"
    default_character_id: str = "aemeath"
    dev_auth_token: str | None = None
    dev_auth_user_id: str = "user_default"
    api_allow_local_default_user: bool = False
    api_enable_prompt_run_log: bool = False
    enable_model_fallback: bool = True
    api_enable_model_fallback: bool = False
    embedding_profile: str | None = None
    embedding_batch_size: int = 16
    embedding_timeout_seconds: float = 30.0
    embedding_max_retries: int = 2
    embedding_retry_backoff_seconds: float = 0.25
    embedding_allow_hash: bool = False
    embedding_local_model_path: Path = Path(".cache/huggingface/BAAI/bge-m3")
    embedding_local_device: str = "auto"
    embedding_local_max_length: int = 8192
    embedding_lazy_load: bool = True
    enable_context_budget: bool = False
    context_budget_max_chars: int = 24_000
    context_budget_recent_message_limit: int = 8
    context_budget_memory_limit: int = 4
    context_budget_evidence_limit: int = 4

    @staticmethod
    def from_env() -> "Settings":
        load_dotenv()

        db_path = Path(os.getenv("AEMEATH_DB_PATH", "data/aemeath.db"))
        recent_limit = int(os.getenv("AEMEATH_RECENT_MESSAGE_LIMIT", "12"))
        use_mock = _parse_bool(os.getenv("AEMEATH_USE_MOCK_LLM", "true"))
        memory_repair_interval = max(
            0.2,
            float(os.getenv("AEMEATH_MEMORY_REPAIR_INTERVAL_SECONDS", "30")),
        )

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
            db_backend=os.getenv("AEMEATH_DB_BACKEND", "sqlite_raw"),
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
            enable_trace_log=_parse_bool(os.getenv("AEMEATH_ENABLE_TRACE_LOG", "false")),
            trace_log_root=Path(os.getenv("AEMEATH_TRACE_LOG_ROOT", ".aemeath/traces")),
            enable_app_log=_parse_bool(os.getenv("AEMEATH_ENABLE_APP_LOG", "false")),
            app_log_root=Path(os.getenv("AEMEATH_APP_LOG_ROOT", ".aemeath/app_logs")),
            memory_embedding_provider=os.getenv("AEMEATH_MEMORY_EMBEDDING_PROVIDER") or None,
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
            memory_repair_batch_size=max(
                1,
                int(os.getenv("AEMEATH_MEMORY_REPAIR_BATCH_SIZE", "32")),
            ),
            memory_repair_interval_seconds=memory_repair_interval,
            memory_repair_max_backoff_seconds=max(
                memory_repair_interval,
                float(os.getenv("AEMEATH_MEMORY_REPAIR_MAX_BACKOFF_SECONDS", "300")),
            ),
            memory_window_size=max(1, int(os.getenv("AEMEATH_MEMORY_WINDOW_SIZE", "12"))),
            enable_memory_window_extraction=_parse_bool(
                os.getenv("AEMEATH_ENABLE_MEMORY_WINDOW_EXTRACTION", "true")
            ),
            default_user_id=os.getenv("AEMEATH_DEFAULT_USER_ID", "user_default"),
            default_character_id=os.getenv("AEMEATH_DEFAULT_CHARACTER_ID", "aemeath"),
            dev_auth_token=os.getenv("AEMEATH_DEV_AUTH_TOKEN") or None,
            dev_auth_user_id=os.getenv("AEMEATH_DEV_AUTH_USER_ID", "user_default"),
            api_allow_local_default_user=_parse_bool(
                os.getenv("AEMEATH_API_ALLOW_LOCAL_DEFAULT_USER", "false")
            ),
            api_enable_prompt_run_log=_parse_bool(
                os.getenv("AEMEATH_API_ENABLE_PROMPT_RUN_LOG", "false")
            ),
            enable_model_fallback=_parse_bool(
                os.getenv("AEMEATH_ENABLE_MODEL_FALLBACK", "true")
            ),
            api_enable_model_fallback=_parse_bool(
                os.getenv("AEMEATH_API_ENABLE_MODEL_FALLBACK", "false")
            ),
            embedding_profile=os.getenv("AEMEATH_EMBEDDING_PROFILE") or None,
            embedding_batch_size=_parse_embedding_int("AEMEATH_EMBEDDING_BATCH_SIZE", "16"),
            embedding_timeout_seconds=_parse_embedding_float(
                "AEMEATH_EMBEDDING_TIMEOUT_SECONDS",
                "30",
            ),
            embedding_max_retries=_parse_embedding_int(
                "AEMEATH_EMBEDDING_MAX_RETRIES",
                "2",
            ),
            embedding_retry_backoff_seconds=_parse_embedding_float(
                "AEMEATH_EMBEDDING_RETRY_BACKOFF_SECONDS",
                "0.25",
            ),
            embedding_allow_hash=_parse_bool(
                os.getenv("AEMEATH_EMBEDDING_ALLOW_HASH", "false")
            ),
            embedding_local_model_path=Path(
                os.getenv(
                    "AEMEATH_EMBEDDING_LOCAL_MODEL_PATH",
                    ".cache/huggingface/BAAI/bge-m3",
                )
            ),
            embedding_local_device=os.getenv("AEMEATH_EMBEDDING_LOCAL_DEVICE", "auto"),
            embedding_local_max_length=_parse_embedding_int(
                "AEMEATH_EMBEDDING_LOCAL_MAX_LENGTH",
                "8192",
            ),
            embedding_lazy_load=_parse_bool(
                os.getenv("AEMEATH_EMBEDDING_LAZY_LOAD", "true")
            ),
            enable_context_budget=_parse_bool(
                os.getenv("AEMEATH_ENABLE_CONTEXT_BUDGET", "false")
            ),
            context_budget_max_chars=int(
                os.getenv("AEMEATH_CONTEXT_BUDGET_MAX_CHARS", "24000")
            ),
            context_budget_recent_message_limit=int(
                os.getenv("AEMEATH_CONTEXT_BUDGET_RECENT_MESSAGE_LIMIT", "8")
            ),
            context_budget_memory_limit=int(
                os.getenv("AEMEATH_CONTEXT_BUDGET_MEMORY_LIMIT", "4")
            ),
            context_budget_evidence_limit=int(
                os.getenv("AEMEATH_CONTEXT_BUDGET_EVIDENCE_LIMIT", "4")
            ),
        )

    def validate(self) -> None:
        if self.db_backend not in {"sqlite_raw", "sqlalchemy"}:
            raise ValueError(
                "AEMEATH_DB_BACKEND must be either 'sqlite_raw' or 'sqlalchemy'."
            )
        from app.embeddings.factory import validate_runtime_options
        from app.embeddings.schemas import EmbeddingRuntimeOptions

        validate_runtime_options(
            EmbeddingRuntimeOptions(
                batch_size=self.embedding_batch_size,
                timeout_seconds=self.embedding_timeout_seconds,
                max_retries=self.embedding_max_retries,
                retry_backoff_seconds=self.embedding_retry_backoff_seconds,
                local_model_path=self.embedding_local_model_path,
                local_device=self.embedding_local_device,
                local_max_length=self.embedding_local_max_length,
                lazy_load=self.embedding_lazy_load,
                allow_hash=self.embedding_allow_hash,
            )
        )
        if self.memory_repair_batch_size <= 0:
            raise ValueError("AEMEATH_MEMORY_REPAIR_BATCH_SIZE must be positive.")
        if self.memory_repair_interval_seconds <= 0:
            raise ValueError("AEMEATH_MEMORY_REPAIR_INTERVAL_SECONDS must be positive.")
        if self.memory_repair_max_backoff_seconds < self.memory_repair_interval_seconds:
            raise ValueError(
                "AEMEATH_MEMORY_REPAIR_MAX_BACKOFF_SECONDS must be at least the repair interval."
            )
        if self.context_budget_max_chars <= 0:
            raise ValueError("AEMEATH_CONTEXT_BUDGET_MAX_CHARS must be positive.")
        for name, value in (
            (
                "AEMEATH_CONTEXT_BUDGET_RECENT_MESSAGE_LIMIT",
                self.context_budget_recent_message_limit,
            ),
            ("AEMEATH_CONTEXT_BUDGET_MEMORY_LIMIT", self.context_budget_memory_limit),
            (
                "AEMEATH_CONTEXT_BUDGET_EVIDENCE_LIMIT",
                self.context_budget_evidence_limit,
            ),
        ):
            if value < 0:
                raise ValueError(f"{name} must be non-negative.")
        if self.use_mock_llm:
            return
        if not self.deepseek_api_key:
            raise ValueError(
                "DEEPSEEK_API_KEY is required when AEMEATH_USE_MOCK_LLM=false."
            )
