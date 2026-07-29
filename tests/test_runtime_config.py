from __future__ import annotations

from dataclasses import replace

import pytest

from app.config import Settings


def test_runtime_guardrail_environment_names_match_public_contract(monkeypatch):
    monkeypatch.setenv("AEMEATH_RUNTIME_MAX_DIALOGUE_TURNS", "7")
    monkeypatch.setenv("AEMEATH_RUNTIME_MAX_LLM_REQUESTS", "6")
    monkeypatch.setenv("AEMEATH_RUNTIME_MAX_EMBEDDING_REQUESTS", "5")
    monkeypatch.setenv("AEMEATH_SEMANTIC_SKIP_LOW_INFORMATION", "false")

    settings = Settings.from_env()

    assert settings.runtime_dialogue_max_concurrency == 7
    assert settings.runtime_llm_max_concurrency == 6
    assert settings.runtime_embedding_max_concurrency == 5
    assert settings.enable_semantic_low_information_skip is False


@pytest.mark.parametrize(
    "field_name",
    [
        "runtime_session_wait_timeout_seconds",
        "api_rate_limit_idle_ttl_seconds",
        "deepseek_read_timeout_seconds",
    ],
)
def test_runtime_guardrail_floating_point_limits_must_be_finite(settings, field_name):
    with pytest.raises(ValueError):
        replace(settings, **{field_name: float("nan")}).validate()


def test_v01_requires_exactly_one_embedding_request_slot(settings):
    with pytest.raises(ValueError, match="must be exactly 1 in v0.1"):
        replace(settings, runtime_embedding_max_concurrency=2).validate()
