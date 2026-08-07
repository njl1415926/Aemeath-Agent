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


def test_stream_runtime_environment_names_match_frozen_defaults(monkeypatch):
    monkeypatch.setenv("AEMEATH_STREAM_RESPONSE_MAX_CHARS", "32000")
    monkeypatch.setenv("AEMEATH_STREAM_RESPONSE_MAX_UTF8_BYTES", "96000")
    monkeypatch.setenv("AEMEATH_STREAM_PUBLIC_DELTA_MAX_UTF8_BYTES", "4000")
    monkeypatch.setenv("AEMEATH_STREAM_PRODUCER_PUT_TIMEOUT_SECONDS", "4")
    monkeypatch.setenv("AEMEATH_STREAM_ORPHAN_RECOVERY_LIMIT", "99")

    settings = Settings.from_env()

    assert settings.stream_response_max_chars == 32000
    assert settings.stream_response_max_utf8_bytes == 96000
    assert settings.stream_public_delta_max_utf8_bytes == 4000
    assert settings.stream_producer_put_timeout_seconds == 4
    assert settings.stream_orphan_recovery_limit == 99


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("stream_response_max_chars", 32_769),
        ("stream_response_max_utf8_bytes", 98_305),
        ("stream_public_delta_max_utf8_bytes", 4_097),
        ("stream_public_delta_max_utf8_bytes", 3),
        ("stream_orphan_recovery_limit", 501),
        ("stream_producer_put_timeout_seconds", 0),
    ],
)
def test_stream_runtime_limits_fail_closed(settings, field_name, value):
    with pytest.raises(ValueError):
        replace(settings, **{field_name: value}).validate()


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
