from __future__ import annotations

from dataclasses import replace

import pytest

from app.errors import AemeathError, ErrorCode
from app.main import build_application_runtime
from app.model.deepseek import FallbackChatModelClient
from app.runtime.guardrails.concurrency import (
    LimitedChatModelClient,
    LimitedStreamingChatModelClient,
)


class _FakeDeepSeek:
    instances = []

    def __init__(self, **_kwargs):
        self.close_count = 0
        type(self).instances.append(self)

    def chat(self, *, messages):
        raise AemeathError(
            code=ErrorCode.MODEL_TIMEOUT,
            public_message="timeout",
            internal_message="model_read_timeout",
            retryable=True,
        )

    def close(self):
        self.close_count += 1


def _remote_settings(settings):
    return replace(
        settings,
        use_mock_llm=False,
        deepseek_api_key="test-only-key",
        enable_long_term_memory=True,
        enable_model_fallback=True,
    )


def test_cli_dialogue_fallback_is_not_reused_by_memory_extraction(settings, monkeypatch):
    _FakeDeepSeek.instances.clear()
    monkeypatch.setattr("app.main.DeepSeekClient", _FakeDeepSeek)
    runtime = build_application_runtime(_remote_settings(settings), surface="cli")
    dialogue = runtime.agent_service.controller.response_generator.chat_model
    extraction = runtime.memory_admin_service.extraction_service.model_client
    assert isinstance(dialogue, LimitedChatModelClient)
    assert isinstance(dialogue.delegate, FallbackChatModelClient)
    assert isinstance(extraction, LimitedChatModelClient)
    assert extraction.delegate is _FakeDeepSeek.instances[0]
    assert dialogue.gate is extraction.gate
    assert "local fallback" in dialogue.chat(messages=[])
    with pytest.raises(AemeathError) as raised:
        extraction.chat(messages=[])
    assert raised.value.code == ErrorCode.MODEL_TIMEOUT
    runtime.close()
    assert _FakeDeepSeek.instances[0].close_count == 1


def test_api_dialogue_and_extraction_both_use_direct_deepseek(settings, monkeypatch):
    _FakeDeepSeek.instances.clear()
    monkeypatch.setattr("app.main.DeepSeekClient", _FakeDeepSeek)
    runtime = build_application_runtime(_remote_settings(settings), surface="api")
    dialogue = runtime.agent_service.controller.response_generator.chat_model
    extraction = runtime.memory_admin_service.extraction_service.model_client
    streaming = runtime.agent_stream_service.model_client
    assert dialogue.delegate is extraction.delegate is _FakeDeepSeek.instances[0]
    assert isinstance(streaming, LimitedStreamingChatModelClient)
    assert streaming.delegate is _FakeDeepSeek.instances[0]
    assert dialogue.gate is extraction.gate is streaming.gate
    runtime.close()


def test_api_fallback_configuration_fails_before_client_creation(settings, monkeypatch):
    _FakeDeepSeek.instances.clear()
    monkeypatch.setattr("app.main.DeepSeekClient", _FakeDeepSeek)
    configured = replace(
        _remote_settings(settings),
        api_enable_model_fallback=True,
    )
    with pytest.raises(AemeathError) as raised:
        build_application_runtime(configured, surface="api")
    assert raised.value.code == ErrorCode.CONFIG_INVALID
    assert raised.value.metadata["reason"] == "api_model_fallback_not_supported"
    assert _FakeDeepSeek.instances == []


def test_api_runtime_closes_stream_registry_before_deepseek_transport(
    settings,
    monkeypatch,
) -> None:
    calls = []

    class TrackingDeepSeek(_FakeDeepSeek):
        def close(self):
            calls.append("deepseek")
            super().close()

    class TrackingRegistry:
        def __init__(self, **_kwargs):
            pass

        def close(self):
            calls.append("registry")

    monkeypatch.setattr("app.main.DeepSeekClient", TrackingDeepSeek)
    monkeypatch.setattr("app.main.ActiveStreamRegistry", TrackingRegistry)
    runtime = build_application_runtime(_remote_settings(settings), surface="api")

    runtime.close()

    assert calls.index("registry") < calls.index("deepseek")
