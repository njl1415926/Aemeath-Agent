from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

from app.deployment.secrets import (
    API_BEARER_SECRET_PATH,
    DEEPSEEK_SECRET_PATH,
    WEB_AUTH_CSRF_SECRET_PATH,
    resolve_secret,
)


def _parse_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_cors_origins(value: str) -> tuple[str, ...]:
    if not value.strip():
        return ()
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("AEMEATH_API_CORS_ALLOWED_ORIGINS must be a JSON array.") from exc
    if not isinstance(parsed, list) or any(not isinstance(item, str) for item in parsed):
        raise ValueError("AEMEATH_API_CORS_ALLOWED_ORIGINS must be a JSON string array.")
    origins = tuple(parsed)
    if len(set(origins)) != len(origins):
        raise ValueError("AEMEATH_API_CORS_ALLOWED_ORIGINS must not contain duplicates.")
    return origins


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
    deepseek_api_key: str | None = field(repr=False)
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
    dev_auth_token: str | None = field(default=None, repr=False)
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
    runtime_session_wait_timeout_seconds: float = 5.0
    runtime_dialogue_max_concurrency: int = 2
    runtime_dialogue_wait_timeout_seconds: float = 0.25
    runtime_llm_max_concurrency: int = 2
    runtime_llm_wait_timeout_seconds: float = 2.0
    runtime_embedding_max_concurrency: int = 1
    runtime_embedding_wait_timeout_seconds: float = 2.0
    api_chat_user_rate_per_minute: int = 12
    api_chat_user_burst: int = 4
    api_chat_ip_rate_per_minute: int = 60
    api_chat_ip_burst: int = 20
    api_rate_limit_idle_ttl_seconds: float = 600.0
    deepseek_connect_timeout_seconds: float = 3.0
    deepseek_read_timeout_seconds: float = 15.0
    deepseek_write_timeout_seconds: float = 5.0
    deepseek_pool_timeout_seconds: float = 1.0
    deepseek_max_retries: int = 1
    deepseek_retry_backoff_seconds: float = 0.25
    deepseek_retry_backoff_max_seconds: float = 2.0
    deepseek_retry_after_max_seconds: float = 5.0
    deepseek_max_output_tokens: int = 1024
    chat_message_max_chars: int = 4000
    chat_message_max_utf8_bytes: int = 12000
    chat_metadata_max_utf8_bytes: int = 8192
    stream_response_max_chars: int = 32_768
    stream_response_max_utf8_bytes: int = 98_304
    stream_public_delta_max_utf8_bytes: int = 4_096
    stream_producer_put_timeout_seconds: float = 5.0
    stream_orphan_recovery_limit: int = 100
    enable_semantic_low_information_skip: bool = True
    environment: str = "dev"
    api_cors_allowed_origins: tuple[str, ...] = ()
    readiness_timeout_seconds: float = 0.5
    readiness_db_timeout_seconds: float = 0.2
    readiness_filesystem_cache_ttl_seconds: float = 5.0
    readiness_min_free_bytes: int = 64 * 1024 * 1024
    deployment_app_root: Path = Path("/opt/aemeath")
    deepseek_api_key_from_file: bool = False
    dev_auth_token_from_file: bool = False
    api_auth_mode: str = "dev_bearer"
    web_public_origin: str = ""
    web_auth_csrf_key: bytes | None = field(default=None, repr=False)
    web_auth_csrf_key_from_file: bool = False
    invitation_ttl_seconds: int = 86_400
    password_reset_ttl_seconds: int = 3_600
    web_session_idle_seconds: int = 604_800
    web_session_absolute_seconds: int = 2_592_000
    web_session_touch_seconds: int = 300
    web_auth_cleanup_batch_size: int = 100
    password_profile: str = "argon2id_owasp_2026_v1"
    password_work_concurrency: int = 1
    password_work_wait_timeout_seconds: float = 1.0
    auth_pre_rate_per_minute: int = 20
    auth_pre_burst: int = 5
    auth_pre_global_rate_per_minute: int = 300
    auth_pre_global_burst: int = 50
    auth_pre_rate_idle_ttl_seconds: float = 600.0
    auth_pre_rate_max_buckets: int = 4096

    @staticmethod
    def from_env() -> "Settings":
        requested_environment = os.environ.get("AEMEATH_ENVIRONMENT", "dev").strip().lower()
        if requested_environment == "dev":
            load_dotenv()
        environment = os.environ.get("AEMEATH_ENVIRONMENT", "dev").strip().lower()
        if environment != requested_environment:
            raise ValueError("AEMEATH_ENVIRONMENT must not be changed by dotenv.")
        if environment == "prod" and "OPENAI_API_KEY" in os.environ:
            raise ValueError("OPENAI_API_KEY is not accepted in prod.")

        deepseek_api_key = resolve_secret(
            "DEEPSEEK_API_KEY",
            "DEEPSEEK_API_KEY_FILE",
            environment=environment,
            approved_file_path=DEEPSEEK_SECRET_PATH,
        )
        if deepseek_api_key is None and environment != "prod":
            deepseek_api_key = os.getenv("OPENAI_API_KEY") or None
        dev_auth_token = resolve_secret(
            "AEMEATH_DEV_AUTH_TOKEN",
            "AEMEATH_DEV_AUTH_TOKEN_FILE",
            environment=environment,
            approved_file_path=API_BEARER_SECRET_PATH,
        )
        web_auth_csrf_key_text = resolve_secret(
            "AEMEATH_WEB_AUTH_CSRF_KEY",
            "AEMEATH_WEB_AUTH_CSRF_KEY_FILE",
            environment=environment,
            approved_file_path=WEB_AUTH_CSRF_SECRET_PATH,
        )
        web_auth_csrf_key = (
            web_auth_csrf_key_text.encode("utf-8")
            if web_auth_csrf_key_text is not None
            else None
        )

        db_path = Path(os.getenv("AEMEATH_DB_PATH", "data/aemeath.db"))
        recent_limit = int(os.getenv("AEMEATH_RECENT_MESSAGE_LIMIT", "12"))
        use_mock = _parse_bool(os.getenv("AEMEATH_USE_MOCK_LLM", "true"))
        memory_repair_interval = max(
            0.2,
            float(os.getenv("AEMEATH_MEMORY_REPAIR_INTERVAL_SECONDS", "30")),
        )

        return Settings(
            deepseek_api_key=deepseek_api_key,
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
            dev_auth_token=dev_auth_token,
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
            runtime_session_wait_timeout_seconds=float(
                os.getenv("AEMEATH_RUNTIME_SESSION_WAIT_TIMEOUT_SECONDS", "5")
            ),
            runtime_dialogue_max_concurrency=int(
                os.getenv("AEMEATH_RUNTIME_MAX_DIALOGUE_TURNS", "2")
            ),
            runtime_dialogue_wait_timeout_seconds=float(
                os.getenv("AEMEATH_RUNTIME_DIALOGUE_WAIT_TIMEOUT_SECONDS", "0.25")
            ),
            runtime_llm_max_concurrency=int(
                os.getenv("AEMEATH_RUNTIME_MAX_LLM_REQUESTS", "2")
            ),
            runtime_llm_wait_timeout_seconds=float(
                os.getenv("AEMEATH_RUNTIME_LLM_WAIT_TIMEOUT_SECONDS", "2")
            ),
            runtime_embedding_max_concurrency=int(
                os.getenv("AEMEATH_RUNTIME_MAX_EMBEDDING_REQUESTS", "1")
            ),
            runtime_embedding_wait_timeout_seconds=float(
                os.getenv("AEMEATH_RUNTIME_EMBEDDING_WAIT_TIMEOUT_SECONDS", "2")
            ),
            api_chat_user_rate_per_minute=int(
                os.getenv("AEMEATH_API_CHAT_USER_RATE_PER_MINUTE", "12")
            ),
            api_chat_user_burst=int(os.getenv("AEMEATH_API_CHAT_USER_BURST", "4")),
            api_chat_ip_rate_per_minute=int(
                os.getenv("AEMEATH_API_CHAT_IP_RATE_PER_MINUTE", "60")
            ),
            api_chat_ip_burst=int(os.getenv("AEMEATH_API_CHAT_IP_BURST", "20")),
            api_rate_limit_idle_ttl_seconds=float(
                os.getenv("AEMEATH_API_RATE_LIMIT_IDLE_TTL_SECONDS", "600")
            ),
            deepseek_connect_timeout_seconds=float(
                os.getenv("DEEPSEEK_CONNECT_TIMEOUT_SECONDS", "3")
            ),
            deepseek_read_timeout_seconds=float(
                os.getenv("DEEPSEEK_READ_TIMEOUT_SECONDS", "15")
            ),
            deepseek_write_timeout_seconds=float(
                os.getenv("DEEPSEEK_WRITE_TIMEOUT_SECONDS", "5")
            ),
            deepseek_pool_timeout_seconds=float(
                os.getenv("DEEPSEEK_POOL_TIMEOUT_SECONDS", "1")
            ),
            deepseek_max_retries=int(os.getenv("DEEPSEEK_MAX_RETRIES", "1")),
            deepseek_retry_backoff_seconds=float(
                os.getenv("DEEPSEEK_RETRY_BACKOFF_SECONDS", "0.25")
            ),
            deepseek_retry_backoff_max_seconds=float(
                os.getenv("DEEPSEEK_RETRY_BACKOFF_MAX_SECONDS", "2")
            ),
            deepseek_retry_after_max_seconds=float(
                os.getenv("DEEPSEEK_RETRY_AFTER_MAX_SECONDS", "5")
            ),
            deepseek_max_output_tokens=int(
                os.getenv("DEEPSEEK_MAX_OUTPUT_TOKENS", "1024")
            ),
            chat_message_max_chars=int(
                os.getenv("AEMEATH_CHAT_MESSAGE_MAX_CHARS", "4000")
            ),
            chat_message_max_utf8_bytes=int(
                os.getenv("AEMEATH_CHAT_MESSAGE_MAX_UTF8_BYTES", "12000")
            ),
            chat_metadata_max_utf8_bytes=int(
                os.getenv("AEMEATH_CHAT_METADATA_MAX_UTF8_BYTES", "8192")
            ),
            stream_response_max_chars=int(
                os.getenv("AEMEATH_STREAM_RESPONSE_MAX_CHARS", "32768")
            ),
            stream_response_max_utf8_bytes=int(
                os.getenv("AEMEATH_STREAM_RESPONSE_MAX_UTF8_BYTES", "98304")
            ),
            stream_public_delta_max_utf8_bytes=int(
                os.getenv("AEMEATH_STREAM_PUBLIC_DELTA_MAX_UTF8_BYTES", "4096")
            ),
            stream_producer_put_timeout_seconds=float(
                os.getenv("AEMEATH_STREAM_PRODUCER_PUT_TIMEOUT_SECONDS", "5")
            ),
            stream_orphan_recovery_limit=int(
                os.getenv("AEMEATH_STREAM_ORPHAN_RECOVERY_LIMIT", "100")
            ),
            enable_semantic_low_information_skip=_parse_bool(
                os.getenv("AEMEATH_SEMANTIC_SKIP_LOW_INFORMATION", "true")
            ),
            environment=environment,
            api_cors_allowed_origins=_parse_cors_origins(
                os.getenv("AEMEATH_API_CORS_ALLOWED_ORIGINS", "[]")
            ),
            readiness_timeout_seconds=float(
                os.getenv("AEMEATH_READINESS_TIMEOUT_SECONDS", "0.5")
            ),
            readiness_db_timeout_seconds=float(
                os.getenv("AEMEATH_READINESS_DB_TIMEOUT_SECONDS", "0.2")
            ),
            readiness_filesystem_cache_ttl_seconds=float(
                os.getenv("AEMEATH_READINESS_FILESYSTEM_CACHE_TTL_SECONDS", "5")
            ),
            readiness_min_free_bytes=int(
                os.getenv("AEMEATH_READINESS_MIN_FREE_BYTES", str(64 * 1024 * 1024))
            ),
            deployment_app_root=Path(
                os.getenv("AEMEATH_DEPLOYMENT_APP_ROOT", "/opt/aemeath")
            ),
            deepseek_api_key_from_file="DEEPSEEK_API_KEY_FILE" in os.environ,
            dev_auth_token_from_file="AEMEATH_DEV_AUTH_TOKEN_FILE" in os.environ,
            api_auth_mode=os.getenv("AEMEATH_API_AUTH_MODE", "dev_bearer").strip(),
            web_public_origin=os.getenv("AEMEATH_WEB_PUBLIC_ORIGIN", "").strip(),
            web_auth_csrf_key=web_auth_csrf_key,
            web_auth_csrf_key_from_file="AEMEATH_WEB_AUTH_CSRF_KEY_FILE" in os.environ,
            invitation_ttl_seconds=int(
                os.getenv("AEMEATH_INVITATION_TTL_SECONDS", "86400")
            ),
            password_reset_ttl_seconds=int(
                os.getenv("AEMEATH_PASSWORD_RESET_TTL_SECONDS", "3600")
            ),
            web_session_idle_seconds=int(
                os.getenv("AEMEATH_WEB_SESSION_IDLE_SECONDS", "604800")
            ),
            web_session_absolute_seconds=int(
                os.getenv("AEMEATH_WEB_SESSION_ABSOLUTE_SECONDS", "2592000")
            ),
            web_session_touch_seconds=int(
                os.getenv("AEMEATH_WEB_SESSION_TOUCH_SECONDS", "300")
            ),
            web_auth_cleanup_batch_size=int(
                os.getenv("AEMEATH_WEB_AUTH_CLEANUP_BATCH_SIZE", "100")
            ),
            password_profile=os.getenv(
                "AEMEATH_PASSWORD_PROFILE", "argon2id_owasp_2026_v1"
            ),
            password_work_concurrency=int(
                os.getenv("AEMEATH_PASSWORD_WORK_CONCURRENCY", "1")
            ),
            password_work_wait_timeout_seconds=float(
                os.getenv("AEMEATH_PASSWORD_WORK_WAIT_TIMEOUT_SECONDS", "1")
            ),
            auth_pre_rate_per_minute=int(
                os.getenv("AEMEATH_AUTH_PRE_RATE_PER_MINUTE", "20")
            ),
            auth_pre_burst=int(os.getenv("AEMEATH_AUTH_PRE_BURST", "5")),
            auth_pre_global_rate_per_minute=int(
                os.getenv("AEMEATH_AUTH_PRE_GLOBAL_RATE_PER_MINUTE", "300")
            ),
            auth_pre_global_burst=int(
                os.getenv("AEMEATH_AUTH_PRE_GLOBAL_BURST", "50")
            ),
            auth_pre_rate_idle_ttl_seconds=float(
                os.getenv("AEMEATH_AUTH_PRE_RATE_IDLE_TTL_SECONDS", "600")
            ),
            auth_pre_rate_max_buckets=int(
                os.getenv("AEMEATH_AUTH_PRE_RATE_MAX_BUCKETS", "4096")
            ),
        )

    def validate(self) -> None:
        if self.environment not in {"dev", "test", "prod"}:
            raise ValueError("AEMEATH_ENVIRONMENT must be dev, test, or prod.")
        if self.db_backend not in {"sqlite_raw", "sqlalchemy"}:
            raise ValueError(
                "AEMEATH_DB_BACKEND must be either 'sqlite_raw' or 'sqlalchemy'."
            )
        if self.api_auth_mode not in {"dev_bearer", "web_session"}:
            raise ValueError(
                "AEMEATH_API_AUTH_MODE must be either dev_bearer or web_session."
            )
        if not 3_600 <= self.invitation_ttl_seconds <= 604_800:
            raise ValueError("AEMEATH_INVITATION_TTL_SECONDS is outside reviewed bounds.")
        if not 300 <= self.password_reset_ttl_seconds <= 86_400:
            raise ValueError(
                "AEMEATH_PASSWORD_RESET_TTL_SECONDS is outside reviewed bounds."
            )
        if not 300 <= self.web_session_idle_seconds <= 604_800:
            raise ValueError("AEMEATH_WEB_SESSION_IDLE_SECONDS is outside reviewed bounds.")
        if not (
            self.web_session_idle_seconds
            <= self.web_session_absolute_seconds
            <= 2_592_000
        ):
            raise ValueError(
                "AEMEATH_WEB_SESSION_ABSOLUTE_SECONDS is outside reviewed bounds."
            )
        if not 60 <= self.web_session_touch_seconds <= 900:
            raise ValueError("AEMEATH_WEB_SESSION_TOUCH_SECONDS is outside reviewed bounds.")
        if self.web_session_touch_seconds >= self.web_session_idle_seconds:
            raise ValueError("AEMEATH_WEB_SESSION_TOUCH_SECONDS must be below idle timeout.")
        if not 1 <= self.web_auth_cleanup_batch_size <= 1000:
            raise ValueError("AEMEATH_WEB_AUTH_CLEANUP_BATCH_SIZE is outside reviewed bounds.")
        if self.password_profile != "argon2id_owasp_2026_v1":
            raise ValueError("AEMEATH_PASSWORD_PROFILE is not approved.")
        if self.password_work_concurrency != 1:
            raise ValueError("AEMEATH_PASSWORD_WORK_CONCURRENCY must be exactly 1 in v0.1.")
        if self.password_work_wait_timeout_seconds < 0:
            raise ValueError("AEMEATH_PASSWORD_WORK_WAIT_TIMEOUT_SECONDS must be non-negative.")
        for name, value in (
            ("AEMEATH_AUTH_PRE_RATE_PER_MINUTE", self.auth_pre_rate_per_minute),
            ("AEMEATH_AUTH_PRE_BURST", self.auth_pre_burst),
            (
                "AEMEATH_AUTH_PRE_GLOBAL_RATE_PER_MINUTE",
                self.auth_pre_global_rate_per_minute,
            ),
            ("AEMEATH_AUTH_PRE_GLOBAL_BURST", self.auth_pre_global_burst),
            ("AEMEATH_AUTH_PRE_RATE_MAX_BUCKETS", self.auth_pre_rate_max_buckets),
        ):
            if isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be a positive integer.")
        if self.auth_pre_rate_idle_ttl_seconds <= 0:
            raise ValueError("AEMEATH_AUTH_PRE_RATE_IDLE_TTL_SECONDS must be positive.")
        if self.api_auth_mode == "web_session":
            self._validate_web_auth()
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
        positive_ints = (
            ("AEMEATH_RUNTIME_MAX_DIALOGUE_TURNS", self.runtime_dialogue_max_concurrency),
            ("AEMEATH_RUNTIME_MAX_LLM_REQUESTS", self.runtime_llm_max_concurrency),
            ("AEMEATH_RUNTIME_MAX_EMBEDDING_REQUESTS", self.runtime_embedding_max_concurrency),
            ("AEMEATH_API_CHAT_USER_RATE_PER_MINUTE", self.api_chat_user_rate_per_minute),
            ("AEMEATH_API_CHAT_USER_BURST", self.api_chat_user_burst),
            ("AEMEATH_API_CHAT_IP_RATE_PER_MINUTE", self.api_chat_ip_rate_per_minute),
            ("AEMEATH_API_CHAT_IP_BURST", self.api_chat_ip_burst),
            ("AEMEATH_CHAT_MESSAGE_MAX_CHARS", self.chat_message_max_chars),
            ("AEMEATH_CHAT_MESSAGE_MAX_UTF8_BYTES", self.chat_message_max_utf8_bytes),
            ("AEMEATH_CHAT_METADATA_MAX_UTF8_BYTES", self.chat_metadata_max_utf8_bytes),
            ("AEMEATH_STREAM_RESPONSE_MAX_CHARS", self.stream_response_max_chars),
            (
                "AEMEATH_STREAM_RESPONSE_MAX_UTF8_BYTES",
                self.stream_response_max_utf8_bytes,
            ),
            (
                "AEMEATH_STREAM_PUBLIC_DELTA_MAX_UTF8_BYTES",
                self.stream_public_delta_max_utf8_bytes,
            ),
            (
                "AEMEATH_STREAM_ORPHAN_RECOVERY_LIMIT",
                self.stream_orphan_recovery_limit,
            ),
        )
        for name, value in positive_ints:
            if isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be a positive integer.")
        if self.runtime_embedding_max_concurrency != 1:
            raise ValueError(
                "AEMEATH_RUNTIME_MAX_EMBEDDING_REQUESTS must be exactly 1 in v0.1."
            )
        if self.stream_public_delta_max_utf8_bytes < 4:
            raise ValueError(
                "AEMEATH_STREAM_PUBLIC_DELTA_MAX_UTF8_BYTES must be at least 4."
            )
        if self.stream_response_max_chars > 32_768:
            raise ValueError("AEMEATH_STREAM_RESPONSE_MAX_CHARS must not exceed 32768.")
        if self.stream_response_max_utf8_bytes > 98_304:
            raise ValueError(
                "AEMEATH_STREAM_RESPONSE_MAX_UTF8_BYTES must not exceed 98304."
            )
        if self.stream_public_delta_max_utf8_bytes > 4_096:
            raise ValueError(
                "AEMEATH_STREAM_PUBLIC_DELTA_MAX_UTF8_BYTES must not exceed 4096."
            )
        if self.stream_orphan_recovery_limit > 500:
            raise ValueError("AEMEATH_STREAM_ORPHAN_RECOVERY_LIMIT must not exceed 500.")
        nonnegative = (
            (
                "AEMEATH_RUNTIME_SESSION_WAIT_TIMEOUT_SECONDS",
                self.runtime_session_wait_timeout_seconds,
            ),
            (
                "AEMEATH_RUNTIME_DIALOGUE_WAIT_TIMEOUT_SECONDS",
                self.runtime_dialogue_wait_timeout_seconds,
            ),
            ("AEMEATH_RUNTIME_LLM_WAIT_TIMEOUT_SECONDS", self.runtime_llm_wait_timeout_seconds),
            (
                "AEMEATH_RUNTIME_EMBEDDING_WAIT_TIMEOUT_SECONDS",
                self.runtime_embedding_wait_timeout_seconds,
            ),
            ("DEEPSEEK_RETRY_BACKOFF_SECONDS", self.deepseek_retry_backoff_seconds),
            ("DEEPSEEK_RETRY_BACKOFF_MAX_SECONDS", self.deepseek_retry_backoff_max_seconds),
            ("DEEPSEEK_RETRY_AFTER_MAX_SECONDS", self.deepseek_retry_after_max_seconds),
        )
        for name, value in nonnegative:
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be non-negative.")
        if (
            not math.isfinite(self.stream_producer_put_timeout_seconds)
            or not 0 < self.stream_producer_put_timeout_seconds <= 60
        ):
            raise ValueError(
                "AEMEATH_STREAM_PRODUCER_PUT_TIMEOUT_SECONDS must be in (0, 60]."
            )
        positive_floats = (
            ("AEMEATH_API_RATE_LIMIT_IDLE_TTL_SECONDS", self.api_rate_limit_idle_ttl_seconds),
            ("DEEPSEEK_CONNECT_TIMEOUT_SECONDS", self.deepseek_connect_timeout_seconds),
            ("DEEPSEEK_READ_TIMEOUT_SECONDS", self.deepseek_read_timeout_seconds),
            ("DEEPSEEK_WRITE_TIMEOUT_SECONDS", self.deepseek_write_timeout_seconds),
            ("DEEPSEEK_POOL_TIMEOUT_SECONDS", self.deepseek_pool_timeout_seconds),
            ("AEMEATH_READINESS_TIMEOUT_SECONDS", self.readiness_timeout_seconds),
            ("AEMEATH_READINESS_DB_TIMEOUT_SECONDS", self.readiness_db_timeout_seconds),
            (
                "AEMEATH_READINESS_FILESYSTEM_CACHE_TTL_SECONDS",
                self.readiness_filesystem_cache_ttl_seconds,
            ),
        )
        for name, value in positive_floats:
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be positive.")
        if self.deepseek_max_retries < 0:
            raise ValueError("DEEPSEEK_MAX_RETRIES must be non-negative.")
        if not 1 <= self.deepseek_max_output_tokens <= 8192:
            raise ValueError("DEEPSEEK_MAX_OUTPUT_TOKENS must be between 1 and 8192.")
        if self.readiness_db_timeout_seconds > self.readiness_timeout_seconds:
            raise ValueError(
                "AEMEATH_READINESS_DB_TIMEOUT_SECONDS must not exceed the total timeout."
            )
        if self.readiness_min_free_bytes < 0:
            raise ValueError("AEMEATH_READINESS_MIN_FREE_BYTES must be non-negative.")
        self._validate_cors_origins()
        if self.environment == "prod":
            self._validate_production()
        if self.use_mock_llm:
            return
        if not self.deepseek_api_key:
            raise ValueError(
                "DEEPSEEK_API_KEY is required when AEMEATH_USE_MOCK_LLM=false."
            )

    def _validate_cors_origins(self) -> None:
        for origin in self.api_cors_allowed_origins:
            parsed = urlsplit(origin)
            if (
                origin == "*"
                or parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("AEMEATH_API_CORS_ALLOWED_ORIGINS contains an invalid origin.")
            if self.environment == "prod" and parsed.scheme != "https":
                raise ValueError("Production CORS origins must use HTTPS.")

    def _validate_web_auth(self) -> None:
        parsed = urlsplit(self.web_public_origin)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
            or self.web_public_origin.endswith("/")
        ):
            raise ValueError("AEMEATH_WEB_PUBLIC_ORIGIN must be an exact origin.")
        if self.environment == "prod" and parsed.scheme != "https":
            raise ValueError("AEMEATH_WEB_PUBLIC_ORIGIN must use HTTPS in prod.")
        if self.api_cors_allowed_origins:
            raise ValueError("Web Session mode requires an empty CORS allowlist.")
        if self.api_allow_local_default_user:
            raise ValueError("Web Session mode rejects the local default account adapter.")
        if self.web_auth_csrf_key is None or len(self.web_auth_csrf_key) < 32:
            raise ValueError("AEMEATH_WEB_AUTH_CSRF_KEY_FILE must provide at least 32 bytes.")

    def _validate_production(self) -> None:
        fixed_paths = {
            "AEMEATH_DB_PATH": (self.db_path, "/var/lib/aemeath/data/aemeath.db"),
            "AEMEATH_MEMORY_VECTOR_DB_PATH": (
                self.memory_vector_db_path,
                "/var/lib/aemeath/data/aemeath_memory.sqlite",
            ),
            "AEMEATH_APP_LOG_ROOT": (self.app_log_root, "/var/log/aemeath/app"),
            "AEMEATH_TRACE_LOG_ROOT": (self.trace_log_root, "/var/log/aemeath/trace"),
            "AEMEATH_PROMPT_RUN_LOG_ROOT": (
                self.prompt_run_log_root,
                "/var/log/aemeath/prompt",
            ),
            "AEMEATH_DEPLOYMENT_APP_ROOT": (self.deployment_app_root, "/opt/aemeath"),
        }
        for name, (actual, expected) in fixed_paths.items():
            if actual.as_posix() != expected:
                raise ValueError(f"{name} must use the approved production path.")
        if self.db_backend != "sqlite_raw":
            raise ValueError("AEMEATH_DB_BACKEND must be sqlite_raw in prod.")
        if self.api_auth_mode != "dev_bearer":
            raise ValueError("The accepted D4 production baseline requires dev_bearer mode.")
        if self.use_mock_llm:
            raise ValueError("AEMEATH_USE_MOCK_LLM must be false in prod.")
        if self.enable_model_fallback or self.api_enable_model_fallback:
            raise ValueError("Model fallback must be disabled in prod.")
        if self.api_allow_local_default_user:
            raise ValueError("AEMEATH_API_ALLOW_LOCAL_DEFAULT_USER must be false in prod.")
        if self.enable_long_term_memory:
            raise ValueError("AEMEATH_ENABLE_LONG_TERM_MEMORY must be false in prod v0.1.")
        if (
            self.embedding_profile is not None
            or self.memory_embedding_provider is not None
            or self.embedding_allow_hash
        ):
            raise ValueError("Embedding providers must remain disabled in prod v0.1.")
        if not self.enable_context_budget:
            raise ValueError("AEMEATH_ENABLE_CONTEXT_BUDGET must be true in prod.")
        if self.enable_prompt_run_log or self.api_enable_prompt_run_log:
            raise ValueError("Prompt run logging must be disabled in prod.")
        if not self.enable_app_log or not self.enable_trace_log:
            raise ValueError("App and trace logging must be enabled in prod.")
        if self.api_cors_allowed_origins:
            raise ValueError("Production CORS allowlist must be empty in v0.1.")
        if not self.dev_auth_token:
            raise ValueError("AEMEATH_DEV_AUTH_TOKEN_FILE is required in prod.")
        if not self.dev_auth_token_from_file:
            raise ValueError("AEMEATH_DEV_AUTH_TOKEN must come from its _FILE source in prod.")
        if (
            not self.default_user_id
            or not self.dev_auth_user_id
            or self.default_user_id == "user_default"
            or self.dev_auth_user_id == "user_default"
        ):
            raise ValueError("Production user IDs must not use user_default.")
        if self.default_user_id != self.dev_auth_user_id:
            raise ValueError("Production default and bearer user IDs must match.")
        if not self.deepseek_api_key:
            raise ValueError("DEEPSEEK_API_KEY_FILE is required in prod.")
        if not self.deepseek_api_key_from_file:
            raise ValueError("DEEPSEEK_API_KEY must come from its _FILE source in prod.")
        base_url = urlsplit(self.deepseek_base_url)
        if (
            base_url.scheme != "https"
            or not base_url.hostname
            or base_url.username is not None
            or base_url.password is not None
            or base_url.query
            or base_url.fragment
        ):
            raise ValueError("DEEPSEEK_BASE_URL must be an HTTPS origin in prod.")
