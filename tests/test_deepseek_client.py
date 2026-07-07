from __future__ import annotations

from io import BytesIO
from urllib import error

import pytest

from app.model.deepseek import DeepSeekClient


def test_deepseek_payload_skips_reasoning_effort_when_thinking_disabled():
    client = DeepSeekClient(
        api_key="test",
        base_url="https://api.deepseek.com",
        model="deepseek-v4-pro",
        thinking="disabled",
        reasoning_effort="high",
    )

    payload = client._build_payload(messages=[{"role": "user", "content": "ping"}])

    assert payload["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in payload


def test_deepseek_payload_keeps_reasoning_effort_when_thinking_enabled():
    client = DeepSeekClient(
        api_key="test",
        base_url="https://api.deepseek.com",
        model="deepseek-v4-pro",
        thinking="enabled",
        reasoning_effort="high",
    )

    payload = client._build_payload(messages=[{"role": "user", "content": "ping"}])

    assert payload["thinking"] == {"type": "enabled"}
    assert payload["reasoning_effort"] == "high"


class _FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return b'{"choices": [{"message": {"content": " pong "}}]}'


def _client_for_retry_tests() -> DeepSeekClient:
    return DeepSeekClient(
        api_key="test",
        base_url="https://api.deepseek.com",
        model="deepseek-v4-pro",
        retry_base_delay_seconds=0,
    )


def test_deepseek_retries_transient_url_errors_three_times(monkeypatch):
    calls = []

    def fake_urlopen(req, timeout):
        calls.append((req, timeout))
        if len(calls) < 4:
            raise error.URLError("timed out")
        return _FakeResponse()

    monkeypatch.setattr("app.model.deepseek.request.urlopen", fake_urlopen)

    assert _client_for_retry_tests().chat(messages=[{"role": "user", "content": "ping"}]) == "pong"
    assert len(calls) == 4


def test_deepseek_retries_retryable_http_status(monkeypatch):
    calls = []

    def fake_urlopen(req, timeout):
        calls.append((req, timeout))
        if len(calls) == 1:
            raise error.HTTPError(
                url=req.full_url,
                code=503,
                msg="Service Unavailable",
                hdrs=None,
                fp=BytesIO(b"temporarily unavailable"),
            )
        return _FakeResponse()

    monkeypatch.setattr("app.model.deepseek.request.urlopen", fake_urlopen)

    assert _client_for_retry_tests().chat(messages=[{"role": "user", "content": "ping"}]) == "pong"
    assert len(calls) == 2


def test_deepseek_does_not_retry_auth_errors(monkeypatch):
    calls = []

    def fake_urlopen(req, timeout):
        calls.append((req, timeout))
        raise error.HTTPError(
            url=req.full_url,
            code=401,
            msg="Unauthorized",
            hdrs=None,
            fp=BytesIO(b"unauthorized"),
        )

    monkeypatch.setattr("app.model.deepseek.request.urlopen", fake_urlopen)

    with pytest.raises(error.HTTPError):
        _client_for_retry_tests().chat(messages=[{"role": "user", "content": "ping"}])
    assert len(calls) == 1
