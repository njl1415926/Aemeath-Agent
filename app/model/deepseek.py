from __future__ import annotations

import math
import time
from threading import Lock
from typing import Any, Callable

import httpx

from app.errors import AemeathError, ErrorCode, map_exception, redact_sensitive
from app.observability.noop import NoopAppLogger
from app.observability.ports import AppLoggerPort
from app.observability.schemas import AppLogRecord
from app.runtime.guardrails.errors import RuntimeBusyError

RETRYABLE_HTTP_STATUS_CODES = {408, 429, 500, 502, 503, 504}
FALLBACK_ERROR_CODES = {
    ErrorCode.MODEL_TIMEOUT,
    ErrorCode.MODEL_RATE_LIMITED,
    ErrorCode.MODEL_UNAVAILABLE,
    ErrorCode.MODEL_INVALID_RESPONSE,
}


class DeepSeekClient:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        thinking: str = "disabled",
        reasoning_effort: str = "",
        connect_timeout_seconds: float = 3.0,
        read_timeout_seconds: float = 15.0,
        write_timeout_seconds: float = 5.0,
        pool_timeout_seconds: float = 1.0,
        max_retries: int = 1,
        retry_backoff_seconds: float = 0.25,
        retry_backoff_max_seconds: float = 2.0,
        retry_after_max_seconds: float = 5.0,
        max_output_tokens: int = 1024,
        max_connections: int = 2,
        client: Any | None = None,
        client_factory: Callable[[], Any] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        app_logger: AppLoggerPort | None = None,
    ):
        if client is not None and client_factory is not None:
            raise _config_error("deepseek_client_ownership_ambiguous")
        if max_retries < 0 or min(
            connect_timeout_seconds,
            read_timeout_seconds,
            write_timeout_seconds,
            pool_timeout_seconds,
        ) <= 0:
            raise _config_error("deepseek_timeout_or_retry_invalid")
        if retry_backoff_seconds < 0 or retry_backoff_max_seconds < 0:
            raise _config_error("deepseek_backoff_invalid")
        if retry_after_max_seconds < 0 or not 1 <= max_output_tokens <= 8192:
            raise _config_error("deepseek_output_or_retry_after_invalid")
        if max_connections <= 0:
            raise _config_error("deepseek_max_connections_invalid")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.thinking = thinking
        self.reasoning_effort = reasoning_effort
        self.max_retries = max_retries
        self.retry_backoff_seconds = retry_backoff_seconds
        self.retry_backoff_max_seconds = retry_backoff_max_seconds
        self.retry_after_max_seconds = retry_after_max_seconds
        self.max_output_tokens = max_output_tokens
        self._sleeper = sleeper
        self._app_logger = app_logger or NoopAppLogger()
        self._owned = client is None
        self._close_lock = Lock()
        self._closed = False
        timeout = httpx.Timeout(
            connect=connect_timeout_seconds,
            read=read_timeout_seconds,
            write=write_timeout_seconds,
            pool=pool_timeout_seconds,
        )
        if client is not None:
            self._client = client
        elif client_factory is not None:
            self._client = client_factory()
        else:
            self._client = httpx.Client(
                timeout=timeout,
                limits=httpx.Limits(
                    max_connections=max_connections,
                    max_keepalive_connections=max_connections,
                    keepalive_expiry=30.0,
                ),
            )

    def _build_payload(self, *, messages: list[dict[str, str]]) -> dict:
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.6,
            "max_tokens": self.max_output_tokens,
        }
        thinking = self.thinking.strip().lower()
        if thinking:
            payload["thinking"] = {"type": thinking}
        reasoning_effort = self.reasoning_effort.strip().lower()
        if thinking == "enabled" and reasoning_effort:
            payload["reasoning_effort"] = reasoning_effort
        return payload

    def chat(self, *, messages: list[dict[str, str]]) -> str:
        started = time.monotonic()
        try:
            result, attempt_count = self._chat(messages=messages)
        except Exception as exc:
            error = map_exception(exc)
            attempt_count = error.metadata.get("attempt_count")
            self._record_terminal(
                event_name="model.deepseek_failed",
                level="error",
                execution_ms=(time.monotonic() - started) * 1000.0,
                attempt_count=attempt_count if isinstance(attempt_count, int) else None,
                error_code=error.code.value,
                limit_reason=error.metadata.get("reason"),
            )
            raise
        self._record_terminal(
            event_name="model.deepseek_completed",
            level="info",
            execution_ms=(time.monotonic() - started) * 1000.0,
            attempt_count=attempt_count,
        )
        return result

    def _chat(self, *, messages: list[dict[str, str]]) -> tuple[str, int]:
        with self._close_lock:
            if self._closed:
                raise _model_error(ErrorCode.MODEL_UNAVAILABLE, "deepseek_client_closed")
        payload = self._build_payload(messages=messages)
        for attempt_index in range(self.max_retries + 1):
            try:
                response = self._client.post(
                    f"{self.base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
            except httpx.PoolTimeout as exc:
                raise RuntimeBusyError("model_connection_pool") from exc
            except httpx.ReadTimeout as exc:
                raise _model_error(
                    ErrorCode.MODEL_TIMEOUT, "model_read_timeout", attempt_index + 1
                ) from exc
            except httpx.WriteTimeout as exc:
                raise _model_error(
                    ErrorCode.MODEL_TIMEOUT, "model_write_timeout", attempt_index + 1
                ) from exc
            except httpx.ConnectTimeout as exc:
                if self._has_retry_left(attempt_index):
                    self._sleep_before_retry(attempt_index, None)
                    continue
                raise _model_error(
                    ErrorCode.MODEL_TIMEOUT, "model_connect_timeout", attempt_index + 1
                ) from exc
            except httpx.ConnectError as exc:
                if self._has_retry_left(attempt_index):
                    self._sleep_before_retry(attempt_index, None)
                    continue
                raise _model_error(
                    ErrorCode.MODEL_UNAVAILABLE, "model_connect_error", attempt_index + 1
                ) from exc
            except httpx.RequestError as exc:
                raise _model_error(
                    ErrorCode.MODEL_UNAVAILABLE, "model_request_error", attempt_index + 1
                ) from exc

            status = response.status_code
            if status == 200:
                return (
                    self._parse_response(response, attempt_count=attempt_index + 1),
                    attempt_index + 1,
                )
            code, reason, retryable = _classify_status(status)
            if retryable and self._has_retry_left(attempt_index):
                self._sleep_before_retry(attempt_index, response)
                continue
            retry_after = self._retry_after(response) if status == 429 else None
            raise _model_error(
                code,
                reason,
                attempt_index + 1,
                retry_after_seconds=retry_after,
            )
        raise AssertionError("unreachable")

    def _record_terminal(
        self,
        *,
        event_name: str,
        level: str,
        execution_ms: float,
        attempt_count: int | None,
        error_code: str | None = None,
        limit_reason: object | None = None,
    ) -> None:
        payload: dict[str, object] = {
            "provider": "deepseek",
            "model": self.model,
            "execution_ms": round(execution_ms, 3),
        }
        if attempt_count is not None:
            payload["attempt_count"] = attempt_count
            payload["retry_count"] = max(0, attempt_count - 1)
        if limit_reason is not None:
            payload["limit_reason"] = str(limit_reason)
        self._app_logger.record(
            AppLogRecord(
                event_name=event_name,
                level=level,
                error_code=error_code,
                payload=redact_sensitive(payload),
            )
        )

    def _parse_response(self, response: Any, *, attempt_count: int) -> str:
        try:
            payload = response.json()
            choices = payload["choices"]
            if not isinstance(choices, list) or not choices:
                raise ValueError
            message = choices[0]["message"]
            content = message["content"]
            if not isinstance(content, str) or not content.strip():
                raise ValueError
            return content.strip()
        except Exception as exc:
            raise _model_error(
                ErrorCode.MODEL_INVALID_RESPONSE,
                "model_response_invalid",
                attempt_count,
            ) from exc

    def _has_retry_left(self, attempt_index: int) -> bool:
        return attempt_index < self.max_retries

    def _sleep_before_retry(self, attempt_index: int, response: Any | None) -> None:
        delay = min(
            self.retry_backoff_max_seconds,
            self.retry_backoff_seconds * (2**attempt_index),
        )
        if response is not None and response.status_code == 429:
            retry_after = self._retry_after(response)
            if retry_after is not None:
                delay = retry_after
        if delay > 0:
            self._sleeper(delay)

    def _retry_after(self, response: Any) -> float | None:
        try:
            value = float(response.headers.get("Retry-After"))
        except (AttributeError, TypeError, ValueError):
            return None
        if not math.isfinite(value) or value < 0:
            return None
        return min(value, self.retry_after_max_seconds)

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            should_close = self._owned
        if should_close:
            self._client.close()

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "base_url": self.base_url,
            "thinking": self.thinking,
            "reasoning_effort": self.reasoning_effort,
            "max_output_tokens": self.max_output_tokens,
            "api_key": "***",
        }

    def __repr__(self) -> str:
        return f"DeepSeekClient(model={self.model}, base_url={self.base_url})"


class FallbackChatModelClient:
    def __init__(self, *, primary, fallback):
        self.primary = primary
        self.fallback = fallback

    def chat(self, *, messages: list[dict[str, str]]) -> str:
        try:
            return self.primary.chat(messages=messages)
        except AemeathError as exc:
            if exc.code not in FALLBACK_ERROR_CODES:
                raise
            return (
                "Model service is temporarily unavailable; using local fallback. "
                f"{self.fallback.chat(messages=messages)}"
            )


def _classify_status(status: int) -> tuple[ErrorCode, str, bool]:
    if status in {400, 404, 422} or 400 <= status < 500 and status not in {408, 429}:
        if status in {401, 403}:
            return ErrorCode.MODEL_AUTH_FAILED, "model_auth_failed", False
        return ErrorCode.MODEL_BAD_REQUEST, "model_bad_request", False
    if status == 408:
        return ErrorCode.MODEL_TIMEOUT, "model_http_timeout", True
    if status == 429:
        return ErrorCode.MODEL_RATE_LIMITED, "model_rate_limited", True
    if status in {500, 502, 503, 504}:
        return ErrorCode.MODEL_UNAVAILABLE, "model_upstream_unavailable", True
    return ErrorCode.MODEL_UNAVAILABLE, "model_unexpected_status", False


def _model_error(
    code: ErrorCode,
    reason: str,
    attempt_count: int | None = None,
    *,
    retry_after_seconds: float | None = None,
) -> AemeathError:
    metadata: dict[str, object] = {"reason": reason}
    if attempt_count is not None:
        metadata["attempt_count"] = attempt_count
    if retry_after_seconds is not None:
        metadata["retry_after_seconds"] = retry_after_seconds
    messages = {
        ErrorCode.MODEL_BAD_REQUEST: "The model request was rejected.",
        ErrorCode.MODEL_AUTH_FAILED: "The model service rejected authentication.",
        ErrorCode.MODEL_RATE_LIMITED: "The model service is rate limited. Please try again later.",
        ErrorCode.MODEL_TIMEOUT: "The model request timed out. Please try again.",
        ErrorCode.MODEL_UNAVAILABLE: "The model service is temporarily unavailable.",
        ErrorCode.MODEL_INVALID_RESPONSE: "The model returned an invalid response.",
    }
    return AemeathError(
        code=code,
        public_message=messages[code],
        internal_message=reason,
        retryable=code in {
            ErrorCode.MODEL_RATE_LIMITED,
            ErrorCode.MODEL_TIMEOUT,
            ErrorCode.MODEL_UNAVAILABLE,
        },
        metadata=metadata,
    )


def _config_error(reason: str) -> AemeathError:
    return AemeathError(
        code=ErrorCode.CONFIG_INVALID,
        public_message="Configuration value is invalid.",
        internal_message=reason,
        metadata={"reason": reason},
    )
