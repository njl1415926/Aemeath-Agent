from __future__ import annotations

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
