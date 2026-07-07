from __future__ import annotations

import json
import time
from urllib import error, request

RETRYABLE_HTTP_STATUS_CODES = {408, 429, 500, 502, 503, 504}


class DeepSeekClient:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        thinking: str = "disabled",
        reasoning_effort: str = "",
        max_retries: int = 3,
        retry_base_delay_seconds: float = 0.5,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.thinking = thinking
        self.reasoning_effort = reasoning_effort
        self.max_retries = max(0, max_retries)
        self.retry_base_delay_seconds = max(0.0, retry_base_delay_seconds)

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
        for attempt_index in range(self.max_retries + 1):
            req = request.Request(
                f"{self.base_url}/chat/completions",
                data=body,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            try:
                with request.urlopen(req, timeout=30) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                break
            except error.HTTPError as exc:
                if not self._should_retry_http_error(exc, attempt_index=attempt_index):
                    raise
                self._sleep_before_retry(attempt_index=attempt_index)
            except (TimeoutError, error.URLError):
                if not self._has_retry_left(attempt_index=attempt_index):
                    raise
                self._sleep_before_retry(attempt_index=attempt_index)
        return data["choices"][0]["message"]["content"].strip()

    def _has_retry_left(self, *, attempt_index: int) -> bool:
        return attempt_index < self.max_retries

    def _should_retry_http_error(
        self,
        exc: error.HTTPError,
        *,
        attempt_index: int,
    ) -> bool:
        return exc.code in RETRYABLE_HTTP_STATUS_CODES and self._has_retry_left(
            attempt_index=attempt_index
        )

    def _sleep_before_retry(self, *, attempt_index: int) -> None:
        if self.retry_base_delay_seconds <= 0:
            return
        time.sleep(self.retry_base_delay_seconds * (2**attempt_index))

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
