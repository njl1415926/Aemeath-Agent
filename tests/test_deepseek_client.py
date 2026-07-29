from __future__ import annotations

import httpx
import pytest

from app.errors import AemeathError, ErrorCode
from app.model.deepseek import DeepSeekClient, FallbackChatModelClient
from app.runtime.guardrails.errors import RuntimeBusyError


class _SequenceClient:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []
        self.close_count = 0

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def close(self):
        self.close_count += 1


def _request() -> httpx.Request:
    return httpx.Request("POST", "https://api.deepseek.com/chat/completions")


def _response(status: int, payload=None, *, headers=None) -> httpx.Response:
    return httpx.Response(
        status,
        json=payload if payload is not None else {},
        headers=headers,
        request=_request(),
    )


def _client(fake, **kwargs) -> DeepSeekClient:
    return DeepSeekClient(
        api_key="test",
        base_url="https://api.deepseek.com",
        model="deepseek-v4-pro",
        client=fake,
        sleeper=lambda _delay: None,
        **kwargs,
    )


def test_owned_httpx_client_uses_thirty_second_keepalive_expiry(monkeypatch):
    captured = {}
    fake = _SequenceClient([])

    def create_client(**kwargs):
        captured.update(kwargs)
        return fake

    monkeypatch.setattr("app.model.deepseek.httpx.Client", create_client)
    client = DeepSeekClient(
        api_key="test",
        base_url="https://api.deepseek.com",
        model="deepseek-v4-pro",
    )

    assert captured["limits"].keepalive_expiry == 30.0
    client.close()
    assert fake.close_count == 1


def test_deepseek_payload_skips_reasoning_effort_and_sets_output_cap():
    client = _client(
        _SequenceClient([]),
        thinking="disabled",
        reasoning_effort="high",
        max_output_tokens=321,
    )
    payload = client._build_payload(messages=[{"role": "user", "content": "ping"}])
    assert payload["thinking"] == {"type": "disabled"}
    assert payload["max_tokens"] == 321
    assert "reasoning_effort" not in payload


def test_deepseek_payload_keeps_reasoning_effort_when_thinking_enabled():
    client = _client(
        _SequenceClient([]), thinking="enabled", reasoning_effort="high"
    )
    payload = client._build_payload(messages=[{"role": "user", "content": "ping"}])
    assert payload["reasoning_effort"] == "high"


def test_deepseek_retries_connect_error_once_then_succeeds():
    fake = _SequenceClient(
        [
            httpx.ConnectError("connect", request=_request()),
            _response(200, {"choices": [{"message": {"content": " pong "}}]}),
        ]
    )
    assert _client(fake, max_retries=1).chat(
        messages=[{"role": "user", "content": "ping"}]
    ) == "pong"
    assert len(fake.calls) == 2


def test_deepseek_does_not_retry_read_timeout():
    fake = _SequenceClient([httpx.ReadTimeout("read", request=_request())])
    with pytest.raises(AemeathError) as raised:
        _client(fake, max_retries=1).chat(messages=[])
    assert raised.value.code == ErrorCode.MODEL_TIMEOUT
    assert len(fake.calls) == 1


def test_deepseek_retries_503_once_and_maps_exhaustion():
    fake = _SequenceClient([_response(503), _response(503)])
    with pytest.raises(AemeathError) as raised:
        _client(fake, max_retries=1).chat(messages=[])
    assert raised.value.code == ErrorCode.MODEL_UNAVAILABLE
    assert raised.value.metadata["attempt_count"] == 2


def test_deepseek_429_caps_retry_after():
    delays = []
    fake = _SequenceClient(
        [
            _response(429, headers={"Retry-After": "99"}),
            _response(429, headers={"Retry-After": "99"}),
        ]
    )
    client = DeepSeekClient(
        api_key="test",
        base_url="https://api.deepseek.com",
        model="deepseek-v4-pro",
        client=fake,
        sleeper=delays.append,
        retry_after_max_seconds=5,
    )
    with pytest.raises(AemeathError) as raised:
        client.chat(messages=[])
    assert raised.value.code == ErrorCode.MODEL_RATE_LIMITED
    assert delays == [5]
    assert raised.value.metadata["retry_after_seconds"] == 5


def test_deepseek_pool_timeout_maps_runtime_busy():
    fake = _SequenceClient([httpx.PoolTimeout("pool", request=_request())])
    with pytest.raises(RuntimeBusyError) as raised:
        _client(fake).chat(messages=[])
    assert raised.value.reason == "model_connection_pool"


@pytest.mark.parametrize("status", [401, 403])
def test_deepseek_does_not_retry_auth_errors(status: int):
    fake = _SequenceClient([_response(status)])
    with pytest.raises(AemeathError) as raised:
        _client(fake).chat(messages=[])
    assert raised.value.code == ErrorCode.MODEL_AUTH_FAILED
    assert len(fake.calls) == 1


def test_deepseek_rejects_malformed_response_without_body_leak():
    fake = _SequenceClient([_response(200, {"secret_body": "NEVER_LOG"})])
    with pytest.raises(AemeathError) as raised:
        _client(fake).chat(messages=[])
    assert raised.value.code == ErrorCode.MODEL_INVALID_RESPONSE
    assert "NEVER_LOG" not in raised.value.internal_message


def test_deepseek_does_not_close_injected_client():
    fake = _SequenceClient([])
    client = _client(fake)
    client.close()
    client.close()
    assert fake.close_count == 0


@pytest.mark.parametrize(
    "code",
    [
        ErrorCode.MODEL_TIMEOUT,
        ErrorCode.MODEL_RATE_LIMITED,
        ErrorCode.MODEL_UNAVAILABLE,
        ErrorCode.MODEL_INVALID_RESPONSE,
    ],
)
def test_fallback_uses_only_frozen_model_error_allowlist(code: ErrorCode):
    class Primary:
        def chat(self, *, messages):
            raise AemeathError(
                code=code,
                public_message="failed",
                internal_message="safe_reason",
            )

    class Fallback:
        def chat(self, *, messages):
            return "fallback"

    result = FallbackChatModelClient(primary=Primary(), fallback=Fallback()).chat(
        messages=[]
    )
    assert result.endswith("fallback")


@pytest.mark.parametrize(
    "code",
    [
        ErrorCode.MODEL_AUTH_FAILED,
        ErrorCode.MODEL_BAD_REQUEST,
        ErrorCode.CONFIG_INVALID,
        ErrorCode.RUNTIME_BUSY,
        ErrorCode.INTERNAL_ERROR,
    ],
)
def test_fallback_does_not_mask_disallowed_errors(code: ErrorCode):
    class Primary:
        def chat(self, *, messages):
            raise AemeathError(
                code=code,
                public_message="failed",
                internal_message="safe_reason",
            )

    with pytest.raises(AemeathError) as raised:
        FallbackChatModelClient(primary=Primary(), fallback=object()).chat(messages=[])
    assert raised.value.code == code
