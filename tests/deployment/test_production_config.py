from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from app.config import Settings
from app.deployment.secrets import resolve_secret


def _configure_prod(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    deepseek_file = tmp_path / "deepseek"
    bearer_file = tmp_path / "bearer"
    csrf_file = tmp_path / "csrf"
    deepseek_file.write_bytes(b"deepseek-test-marker\n")
    bearer_file.write_bytes(b"bearer-test-marker\r\n")
    csrf_file.write_bytes(b"csrf-test-marker-with-at-least-32-bytes")
    monkeypatch.setattr("app.config.DEEPSEEK_SECRET_PATH", deepseek_file)
    monkeypatch.setattr("app.config.API_BEARER_SECRET_PATH", bearer_file)
    monkeypatch.setattr("app.config.WEB_AUTH_CSRF_SECRET_PATH", csrf_file)
    for name in (
        "DEEPSEEK_API_KEY",
        "OPENAI_API_KEY",
        "AEMEATH_DEV_AUTH_TOKEN",
        "SILICONFLOW_API_KEY",
        "SILICONFLOW_API_KEY_FILE",
        "AEMEATH_EMBEDDING_PROFILE",
        "AEMEATH_WEB_AUTH_CSRF_KEY",
        "AEMEATH_WEB_AUTH_CSRF_KEY_FILE",
    ):
        monkeypatch.delenv(name, raising=False)
    values = {
        "AEMEATH_ENVIRONMENT": "prod",
        "DEEPSEEK_API_KEY_FILE": str(deepseek_file),
        "AEMEATH_DEV_AUTH_TOKEN_FILE": str(bearer_file),
        "AEMEATH_USE_MOCK_LLM": "false",
        "AEMEATH_DB_BACKEND": "sqlite_raw",
        "AEMEATH_DB_PATH": "/var/lib/aemeath/data/aemeath.db",
        "AEMEATH_MEMORY_VECTOR_DB_PATH": "/var/lib/aemeath/data/aemeath_memory.sqlite",
        "AEMEATH_APP_LOG_ROOT": "/var/log/aemeath/app",
        "AEMEATH_TRACE_LOG_ROOT": "/var/log/aemeath/trace",
        "AEMEATH_PROMPT_RUN_LOG_ROOT": "/var/log/aemeath/prompt",
        "AEMEATH_DEPLOYMENT_APP_ROOT": "/opt/aemeath",
        "AEMEATH_ENABLE_MODEL_FALLBACK": "false",
        "AEMEATH_API_ENABLE_MODEL_FALLBACK": "false",
        "AEMEATH_ENABLE_LONG_TERM_MEMORY": "false",
        "AEMEATH_EMBEDDING_ALLOW_HASH": "false",
        "AEMEATH_ENABLE_CONTEXT_BUDGET": "true",
        "AEMEATH_ENABLE_PROMPT_RUN_LOG": "false",
        "AEMEATH_API_ENABLE_PROMPT_RUN_LOG": "false",
        "AEMEATH_ENABLE_APP_LOG": "true",
        "AEMEATH_ENABLE_TRACE_LOG": "true",
        "AEMEATH_API_ALLOW_LOCAL_DEFAULT_USER": "false",
        "AEMEATH_API_CORS_ALLOWED_ORIGINS": "[]",
        "AEMEATH_DEFAULT_USER_ID": "closed_test_user",
        "AEMEATH_DEV_AUTH_USER_ID": "closed_test_user",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    settings = Settings.from_env()
    settings.validate()
    return settings


def _configure_prod_web(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    settings = _configure_prod(monkeypatch, tmp_path)
    csrf_file = tmp_path / "csrf"
    monkeypatch.delenv("AEMEATH_DEV_AUTH_TOKEN_FILE", raising=False)
    monkeypatch.setenv("AEMEATH_API_AUTH_MODE", "web_session")
    monkeypatch.setenv("AEMEATH_WEB_PUBLIC_ORIGIN", "https://chat.example.test")
    monkeypatch.setenv("AEMEATH_WEB_AUTH_CSRF_KEY_FILE", str(csrf_file))
    monkeypatch.setenv("AEMEATH_USAGE_QUOTA_ENABLED", "true")
    monkeypatch.setenv("AEMEATH_USAGE_QUOTA_DAILY_TURN_LIMIT", "50")
    monkeypatch.setenv("AEMEATH_USAGE_QUOTA_DAILY_TOKEN_LIMIT", "200000")
    settings = Settings.from_env()
    settings.validate()
    return settings


@pytest.mark.parametrize("terminator", [b"\n", b"\r\n"])
def test_secret_file_removes_exactly_one_line_terminator(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    terminator: bytes,
):
    secret_file = tmp_path / "secret"
    secret_file.write_bytes(b" value with spaces " + terminator)
    monkeypatch.setenv("EXAMPLE_SECRET_FILE", str(secret_file))

    value = resolve_secret("EXAMPLE_SECRET", "EXAMPLE_SECRET_FILE", environment="test")

    assert value == " value with spaces "


@pytest.mark.parametrize(
    "raw",
    [b"", b"\n", b"secret\n\n", b"secret\r", b"secret\x00", b"\xff"],
)
def test_secret_file_rejects_invalid_content(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    raw: bytes,
):
    secret_file = tmp_path / "secret"
    secret_file.write_bytes(raw)
    monkeypatch.setenv("EXAMPLE_SECRET_FILE", str(secret_file))

    with pytest.raises(ValueError):
        resolve_secret("EXAMPLE_SECRET", "EXAMPLE_SECRET_FILE", environment="test")


def test_secret_resolver_rejects_dual_source_without_leaking_value(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    marker = "unique-dual-source-marker"
    secret_file = tmp_path / "secret"
    secret_file.write_text(marker, encoding="utf-8")
    monkeypatch.setenv("EXAMPLE_SECRET", marker)
    monkeypatch.setenv("EXAMPLE_SECRET_FILE", str(secret_file))

    with pytest.raises(ValueError) as raised:
        resolve_secret("EXAMPLE_SECRET", "EXAMPLE_SECRET_FILE", environment="prod")

    assert marker not in str(raised.value)


def test_secret_resolver_rejects_oversized_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    secret_file = tmp_path / "secret"
    secret_file.write_bytes(b"x" * 8193)
    monkeypatch.setenv("EXAMPLE_SECRET_FILE", str(secret_file))

    with pytest.raises(ValueError, match="size limit"):
        resolve_secret("EXAMPLE_SECRET", "EXAMPLE_SECRET_FILE", environment="test")


def test_prod_secret_resolver_rejects_plaintext_without_leaking_value(
    monkeypatch: pytest.MonkeyPatch,
):
    marker = "unique-plaintext-secret-marker"
    monkeypatch.setenv("EXAMPLE_SECRET", marker)

    with pytest.raises(ValueError) as raised:
        resolve_secret("EXAMPLE_SECRET", "EXAMPLE_SECRET_FILE", environment="prod")

    assert marker not in str(raised.value)


def test_prod_secret_resolver_requires_approved_file_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    configured = tmp_path / "configured"
    approved = tmp_path / "approved"
    configured.write_text("unique-path-marker", encoding="utf-8")
    monkeypatch.setenv("EXAMPLE_SECRET_FILE", str(configured))

    with pytest.raises(ValueError, match="approved production path"):
        resolve_secret(
            "EXAMPLE_SECRET",
            "EXAMPLE_SECRET_FILE",
            environment="prod",
            approved_file_path=approved,
        )


def test_prod_configuration_accepts_only_frozen_api_profile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    settings = _configure_prod(monkeypatch, tmp_path)

    assert settings.environment == "prod"
    assert settings.enable_long_term_memory is False
    assert settings.enable_context_budget is True
    assert settings.api_cors_allowed_origins == ()
    assert "deepseek-test-marker" not in repr(settings)
    assert "bearer-test-marker" not in repr(settings)


def test_prod_configuration_accepts_web_session_profile_without_bearer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    settings = _configure_prod_web(monkeypatch, tmp_path)

    assert settings.api_auth_mode == "web_session"
    assert settings.dev_auth_token is None
    assert settings.web_auth_csrf_key_from_file is True
    assert settings.web_public_origin == "https://chat.example.test"
    assert settings.usage_quota_daily_token_limit == 200_000
    assert "csrf-test-marker" not in repr(settings)


def test_prod_web_session_rejects_dev_bearer_credential(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    settings = _configure_prod_web(monkeypatch, tmp_path)

    with pytest.raises(ValueError, match="rejects dev Bearer"):
        replace(
            settings,
            dev_auth_token="forbidden",
            dev_auth_token_from_file=True,
        ).validate()


@pytest.mark.parametrize(
    ("field_name", "unsafe_value"),
    [
        ("usage_quota_enabled", False),
        ("usage_quota_daily_turn_limit", 51),
        ("usage_quota_daily_token_limit", 200_001),
        ("usage_quota_estimator_version", "other"),
    ],
)
def test_prod_web_session_rejects_unapproved_quota_profile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    field_name: str,
    unsafe_value: object,
):
    settings = _configure_prod_web(monkeypatch, tmp_path)

    with pytest.raises(ValueError):
        replace(settings, **{field_name: unsafe_value}).validate()


@pytest.mark.parametrize(
    ("field_name", "unsafe_value"),
    [
        ("use_mock_llm", True),
        ("enable_model_fallback", True),
        ("api_enable_model_fallback", True),
        ("api_allow_local_default_user", True),
        ("enable_long_term_memory", True),
        ("embedding_allow_hash", True),
        ("memory_embedding_provider", "hash"),
        ("enable_context_budget", False),
        ("enable_prompt_run_log", True),
        ("api_enable_prompt_run_log", True),
        ("api_cors_allowed_origins", ("https://example.test",)),
        ("db_backend", "sqlalchemy"),
        ("db_path", Path("relative.db")),
        ("deepseek_base_url", "http://api.deepseek.com"),
        ("deepseek_api_key_from_file", False),
        ("dev_auth_token_from_file", False),
    ],
)
def test_prod_configuration_rejects_unsafe_modes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    field_name: str,
    unsafe_value: object,
):
    settings = _configure_prod(monkeypatch, tmp_path)

    with pytest.raises(ValueError):
        replace(settings, **{field_name: unsafe_value}).validate()


def test_prod_rejects_legacy_openai_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    _configure_prod(monkeypatch, tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "legacy-secret-marker")

    with pytest.raises(ValueError) as raised:
        Settings.from_env()

    assert "legacy-secret-marker" not in str(raised.value)


def test_test_environment_does_not_load_dotenv(monkeypatch: pytest.MonkeyPatch):
    called = False

    def _unexpected_dotenv() -> None:
        nonlocal called
        called = True

    monkeypatch.setenv("AEMEATH_ENVIRONMENT", "test")
    monkeypatch.setattr("app.config.load_dotenv", _unexpected_dotenv)

    Settings.from_env()

    assert called is False


def test_dotenv_cannot_switch_dev_environment_to_test(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AEMEATH_ENVIRONMENT", "dev")

    def _switch_environment() -> None:
        monkeypatch.setenv("AEMEATH_ENVIRONMENT", "test")

    monkeypatch.setattr("app.config.load_dotenv", _switch_environment)

    with pytest.raises(ValueError, match="must not be changed by dotenv"):
        Settings.from_env()
