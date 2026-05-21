from __future__ import annotations

import json
from urllib import error, request


class DeepSeekClient:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        thinking: str = "disabled",
        reasoning_effort: str = "",
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.thinking = thinking
        self.reasoning_effort = reasoning_effort

    def _build_payload(self, *, messages: list[dict[str, str]]) -> dict:
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.6,
        }

        thinking = self.thinking.strip().lower()
        if thinking:
            payload["thinking"] = {"type": thinking}

        reasoning_effort = self.reasoning_effort.strip().lower()
        if thinking == "enabled" and reasoning_effort:
            payload["reasoning_effort"] = reasoning_effort

        return payload

    def chat(self, *, messages: list[dict[str, str]]) -> str:
        payload = self._build_payload(messages=messages)
        body = json.dumps(payload).encode("utf-8")
        req = request.Request(
            f"{self.base_url}/chat/completions",
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"].strip()

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "base_url": self.base_url,
            "thinking": self.thinking,
            "reasoning_effort": self.reasoning_effort,
            "api_key": "***",
        }

    def __repr__(self) -> str:
        return f"DeepSeekClient(model={self.model}, base_url={self.base_url})"


class FallbackChatModelClient:
    def __init__(self, *, primary: DeepSeekClient, fallback):
        self.primary = primary
        self.fallback = fallback

    def chat(self, *, messages: list[dict[str, str]]) -> str:
        try:
            return self.primary.chat(messages=messages)
        except (error.URLError, error.HTTPError, KeyError, json.JSONDecodeError):
            return (
                "模型服务暂时不可用，我先保证对话不中断。 "
                f"{self.fallback.chat(messages=messages)}"
            )
