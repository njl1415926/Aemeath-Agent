from __future__ import annotations

import json
import math
import time
from contextlib import ExitStack, contextmanager
from threading import Lock
from typing import Any, Callable, Iterator

import httpx

from app.errors import AemeathError, ErrorCode, map_exception, redact_sensitive
from app.model.streaming import (
    ModelStreamEvent,
    ModelStreamFinished,
    ModelStreamRequest,
    ModelTextDelta,
)
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

    def _build_stream_payload(self, *, messages: list[dict[str, str]]) -> dict:
        payload = self._build_payload(messages=messages)
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": False}
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

    @contextmanager
    def open_stream(
        self,
        *,
        request: ModelStreamRequest,
    ) -> Iterator[DeepSeekModelStreamSession]:
        with self._close_lock:
            if self._closed:
                raise _model_error(ErrorCode.MODEL_UNAVAILABLE, "deepseek_client_closed")
        payload = self._build_stream_payload(messages=request.messages)
        for attempt_index in range(self.max_retries + 1):
            stack = ExitStack()
            try:
                response = stack.enter_context(
                    self._client.stream(
                        "POST",
                        f"{self.base_url}/chat/completions",
                        headers={
                            "Authorization": f"Bearer {self.api_key}",
                            "Content-Type": "application/json",
                        },
                        json=payload,
                    )
                )
            except httpx.PoolTimeout as exc:
                stack.close()
                raise RuntimeBusyError("model_connection_pool") from exc
            except httpx.ReadTimeout as exc:
                stack.close()
                raise _model_error(
                    ErrorCode.MODEL_TIMEOUT,
                    "model_read_timeout",
                    attempt_index + 1,
                ) from exc
            except httpx.WriteTimeout as exc:
                stack.close()
                raise _model_error(
                    ErrorCode.MODEL_TIMEOUT,
                    "model_write_timeout",
                    attempt_index + 1,
                ) from exc
            except httpx.ConnectTimeout as exc:
                stack.close()
                if self._has_retry_left(attempt_index):
                    self._sleep_before_retry(attempt_index, None)
                    continue
                raise _model_error(
                    ErrorCode.MODEL_TIMEOUT,
                    "model_connect_timeout",
                    attempt_index + 1,
                ) from exc
            except httpx.ConnectError as exc:
                stack.close()
                if self._has_retry_left(attempt_index):
                    self._sleep_before_retry(attempt_index, None)
                    continue
                raise _model_error(
                    ErrorCode.MODEL_UNAVAILABLE,
                    "model_connect_error",
                    attempt_index + 1,
                ) from exc
            except httpx.RequestError as exc:
                stack.close()
                raise _model_error(
                    ErrorCode.MODEL_UNAVAILABLE,
                    "model_request_error",
                    attempt_index + 1,
                ) from exc
            except Exception as exc:
                stack.close()
                raise _model_error(
                    ErrorCode.MODEL_UNAVAILABLE,
                    "model_request_error",
                    attempt_index + 1,
                ) from exc

            status = response.status_code
            if status == 200:
                session = DeepSeekModelStreamSession(
                    response=response,
                    attempt_count=attempt_index + 1,
                    close_response=stack.close,
                )
                try:
                    yield session
                finally:
                    session.close()
                return

            code, reason, retryable = _classify_status(status)
            retry_after = self._retry_after(response) if status == 429 else None
            stack.close()
            if retryable and self._has_retry_left(attempt_index):
                self._sleep_before_retry(attempt_index, response)
                continue
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


class DeepSeekSSEParser:
    def __init__(self, *, attempt_count: int) -> None:
        self._attempt_count = attempt_count
        self._content_parts: list[str] = []
        self._finish_reason: str | None = None
        self._done = False

    def feed_data(self, data: str) -> tuple[ModelStreamEvent, ...]:
        if self._done:
            raise self._invalid("model_stream_data_after_done")
        if data == "[DONE]":
            if self._finish_reason is None:
                raise self._invalid("model_finish_reason_missing")
            if not "".join(self._content_parts).strip():
                raise self._invalid("model_response_blank")
            self._done = True
            finish_kind = (
                "complete" if self._finish_reason == "stop" else "output_limit"
            )
            return (ModelStreamFinished(finish_kind=finish_kind),)
        try:
            payload = json.loads(data)
            if not isinstance(payload, dict):
                raise ValueError
            choices = payload["choices"]
            if not isinstance(choices, list):
                raise ValueError
            if not choices:
                if not isinstance(payload.get("usage"), dict):
                    raise ValueError
                return ()
            if len(choices) != 1:
                raise ValueError
            choice = choices[0]
            if not isinstance(choice, dict) or choice.get("index") != 0:
                raise ValueError
            if self._finish_reason is not None:
                raise self._invalid("model_stream_data_after_finish_reason")
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                raise ValueError
            tool_calls = delta.get("tool_calls")
            if tool_calls not in (None, []):
                raise self._invalid("model_tool_calls_unsupported")
            content = delta.get("content")
            if content is not None and not isinstance(content, str):
                raise ValueError
            finish_reason = choice.get("finish_reason")
            if finish_reason is not None and not isinstance(finish_reason, str):
                raise ValueError
        except AemeathError:
            raise
        except Exception as exc:
            raise self._invalid("model_stream_chunk_invalid") from exc

        events: list[ModelStreamEvent] = []
        if content:
            self._content_parts.append(content)
            events.append(ModelTextDelta(text=content))
        if finish_reason is not None:
            self._accept_finish_reason(finish_reason)
        return tuple(events)

    def finalize(self) -> None:
        if not self._done:
            raise self._invalid("model_stream_incomplete")

    def _accept_finish_reason(self, finish_reason: str) -> None:
        if finish_reason in {"stop", "length"}:
            self._finish_reason = finish_reason
            return
        if finish_reason == "content_filter":
            raise self._invalid("model_content_filtered")
        if finish_reason == "tool_calls":
            raise self._invalid("model_tool_calls_unsupported")
        if finish_reason == "insufficient_system_resource":
            raise _model_error(
                ErrorCode.MODEL_UNAVAILABLE,
                "model_insufficient_system_resource",
                self._attempt_count,
            )
        raise self._invalid("model_finish_reason_invalid")

    def _invalid(self, reason: str) -> AemeathError:
        return _model_error(
            ErrorCode.MODEL_INVALID_RESPONSE,
            reason,
            self._attempt_count,
        )


class DeepSeekModelStreamSession:
    def __init__(
        self,
        *,
        response: Any,
        attempt_count: int,
        close_response: Callable[[], None],
    ) -> None:
        self._response = response
        self._attempt_count = attempt_count
        self._close_response = close_response
        self._lock = Lock()
        self._closed = False
        self._cancelled = False
        self._iterated = False

    def __iter__(self) -> Iterator[ModelStreamEvent]:
        with self._lock:
            if self._iterated:
                raise RuntimeError("model_stream_session_already_iterated")
            self._iterated = True
        parser = DeepSeekSSEParser(attempt_count=self._attempt_count)
        try:
            for data in _iter_sse_data(
                self._response.iter_lines(),
                attempt_count=self._attempt_count,
            ):
                with self._lock:
                    if self._cancelled or self._closed:
                        return
                for event in parser.feed_data(data):
                    yield event
                    if isinstance(event, ModelStreamFinished):
                        return
            parser.finalize()
        except httpx.ReadTimeout as exc:
            if self._is_cancelled_or_closed():
                return
            raise _model_error(
                ErrorCode.MODEL_TIMEOUT,
                "model_read_timeout",
                self._attempt_count,
            ) from exc
        except httpx.RequestError as exc:
            if self._is_cancelled_or_closed():
                return
            raise _model_error(
                ErrorCode.MODEL_UNAVAILABLE,
                "model_stream_read_error",
                self._attempt_count,
            ) from exc
        except AemeathError:
            raise
        except Exception as exc:
            if self._is_cancelled_or_closed():
                return
            raise _model_error(
                ErrorCode.MODEL_UNAVAILABLE,
                "model_stream_read_error",
                self._attempt_count,
            ) from exc

    def cancel(self, *, reason: str) -> None:
        del reason
        with self._lock:
            self._cancelled = True
        self.close()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._close_response()

    def _is_cancelled_or_closed(self) -> bool:
        with self._lock:
            return self._cancelled or self._closed


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


def _iter_sse_data(
    lines: Iterator[str],
    *,
    attempt_count: int,
) -> Iterator[str]:
    data_lines: list[str] = []
    for line in lines:
        if not isinstance(line, str):
            raise _model_error(
                ErrorCode.MODEL_INVALID_RESPONSE,
                "model_stream_line_invalid",
                attempt_count,
            )
        if line == "":
            if data_lines:
                yield "\n".join(data_lines)
                data_lines.clear()
            continue
        if line.startswith(":"):
            continue
        if not line.startswith("data:"):
            raise _model_error(
                ErrorCode.MODEL_INVALID_RESPONSE,
                "model_stream_field_invalid",
                attempt_count,
            )
        value = line[5:]
        if value.startswith(" "):
            value = value[1:]
        data_lines.append(value)
    if data_lines:
        yield "\n".join(data_lines)


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
