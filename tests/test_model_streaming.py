from __future__ import annotations

import json
from contextlib import AbstractContextManager
from typing import Any

import httpx
import pytest

from app.errors import AemeathError, ErrorCode
from app.model.deepseek import DeepSeekClient
from app.model.mock_streaming import MockStreamingChatModelClient
from app.model.streaming import (
    ModelStreamFinished,
    ModelStreamRequest,
    ModelTextDelta,
)


class _StreamResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        lines: list[str] | None = None,
        line_error: Exception | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self.lines = lines or []
        self.line_error = line_error
        self.headers = headers or {}
        self.close_count = 0

    def iter_lines(self):
        yield from self.lines
        if self.line_error is not None:
            raise self.line_error

    def close(self) -> None:
        self.close_count += 1


class _StreamContext(AbstractContextManager[_StreamResponse]):
    def __init__(self, outcome: _StreamResponse | BaseException) -> None:
        self.outcome = outcome

    def __enter__(self) -> _StreamResponse:
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome

    def __exit__(self, *_args: Any) -> None:
        if isinstance(self.outcome, _StreamResponse):
            self.outcome.close()


class _StreamingClient:
    def __init__(self, outcomes: list[_StreamResponse | BaseException]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.close_count = 0

    def stream(self, method: str, url: str, **kwargs: Any) -> _StreamContext:
        self.calls.append((method, url, kwargs))
        return _StreamContext(self.outcomes.pop(0))

    def close(self) -> None:
        self.close_count += 1


def _request() -> httpx.Request:
    return httpx.Request("POST", "https://api.deepseek.com/chat/completions")


def _data(payload: dict[str, Any] | str) -> list[str]:
    encoded = payload if isinstance(payload, str) else json.dumps(payload)
    return [f"data: {encoded}", ""]


def _chunk(
    *,
    content: str | None = None,
    reasoning_content: str | None = None,
    finish_reason: str | None = None,
    index: int = 0,
    tool_calls: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    delta: dict[str, Any] = {"content": content}
    if reasoning_content is not None:
        delta["reasoning_content"] = reasoning_content
    if tool_calls is not None:
        delta["tool_calls"] = tool_calls
    return {
        "choices": [
            {
                "index": index,
                "delta": delta,
                "finish_reason": finish_reason,
            }
        ]
    }


def _deepseek(fake: _StreamingClient, **kwargs: Any) -> DeepSeekClient:
    return DeepSeekClient(
        api_key="test-key",
        base_url="https://api.deepseek.com",
        model="deepseek-v4-pro",
        client=fake,
        sleeper=lambda _delay: None,
        **kwargs,
    )


def _stream_request() -> ModelStreamRequest:
    return ModelStreamRequest(messages=[{"role": "user", "content": "hello"}])


@pytest.mark.parametrize(
    ("finish_reason", "finish_kind"),
    [("stop", "complete"), ("length", "output_limit")],
)
def test_deepseek_stream_requires_finish_reason_and_done(
    finish_reason: str,
    finish_kind: str,
) -> None:
    response = _StreamResponse(
        lines=[
            *_data(_chunk(content="Hello")),
            *_data(_chunk(content=" world", finish_reason=finish_reason)),
            *_data("[DONE]"),
        ]
    )
    fake = _StreamingClient([response])

    with _deepseek(fake).open_stream(request=_stream_request()) as session:
        events = list(session)

    assert events == [
        ModelTextDelta("Hello"),
        ModelTextDelta(" world"),
        ModelStreamFinished(finish_kind),
    ]
    assert fake.calls[0][2]["json"]["stream"] is True
    assert fake.calls[0][2]["json"]["max_tokens"] == 1024
    assert response.close_count == 1


def test_reasoning_content_is_never_emitted_or_retained() -> None:
    reasoning_marker = "PRIVATE_REASONING_MARKER_8f4a"
    response = _StreamResponse(
        lines=[
            *_data(_chunk(reasoning_content=reasoning_marker)),
            *_data(_chunk(content="public answer", finish_reason="stop")),
            *_data("[DONE]"),
        ]
    )

    with _deepseek(_StreamingClient([response])).open_stream(
        request=_stream_request()
    ) as session:
        events = list(session)

    assert events == [
        ModelTextDelta("public answer"),
        ModelStreamFinished("complete"),
    ]
    assert reasoning_marker not in repr(events)


def test_deepseek_stream_ignores_explicit_usage_only_chunk() -> None:
    response = _StreamResponse(
        lines=[
            *_data(_chunk(content="answer", finish_reason="stop")),
            *_data({"choices": [], "usage": {"completion_tokens": 1}}),
            *_data("[DONE]"),
        ]
    )

    with _deepseek(_StreamingClient([response])).open_stream(
        request=_stream_request()
    ) as session:
        events = list(session)

    assert events == [
        ModelTextDelta("answer"),
        ModelStreamFinished("complete"),
    ]


def test_deepseek_stream_rejects_empty_choices_without_usage() -> None:
    response = _StreamResponse(lines=_data({"choices": []}))

    with pytest.raises(AemeathError) as raised:
        with _deepseek(_StreamingClient([response])).open_stream(
            request=_stream_request()
        ) as session:
            list(session)

    assert raised.value.code == ErrorCode.MODEL_INVALID_RESPONSE
    assert raised.value.metadata["reason"] == "model_stream_chunk_invalid"


@pytest.mark.parametrize(
    ("finish_reason", "code", "reason", "retryable"),
    [
        (
            "content_filter",
            ErrorCode.MODEL_INVALID_RESPONSE,
            "model_content_filtered",
            False,
        ),
        (
            "tool_calls",
            ErrorCode.MODEL_INVALID_RESPONSE,
            "model_tool_calls_unsupported",
            False,
        ),
        (
            "insufficient_system_resource",
            ErrorCode.MODEL_UNAVAILABLE,
            "model_insufficient_system_resource",
            True,
        ),
        (
            "future_reason",
            ErrorCode.MODEL_INVALID_RESPONSE,
            "model_finish_reason_invalid",
            False,
        ),
    ],
)
def test_deepseek_stream_maps_unsupported_finish_reasons_safely(
    finish_reason: str,
    code: ErrorCode,
    reason: str,
    retryable: bool,
) -> None:
    response = _StreamResponse(
        lines=_data(_chunk(content="partial", finish_reason=finish_reason))
    )

    with pytest.raises(AemeathError) as raised:
        with _deepseek(_StreamingClient([response])).open_stream(
            request=_stream_request()
        ) as session:
            list(session)

    assert raised.value.code == code
    assert raised.value.metadata["reason"] == reason
    assert raised.value.retryable is retryable
    assert "partial" not in raised.value.internal_message
    assert response.close_count == 1


def test_deepseek_stream_rejects_tool_call_delta() -> None:
    response = _StreamResponse(
        lines=_data(
            _chunk(
                tool_calls=[
                    {
                        "id": "call_private",
                        "type": "function",
                        "function": {"name": "private_tool", "arguments": "{}"},
                    }
                ]
            )
        )
    )

    with pytest.raises(AemeathError) as raised:
        with _deepseek(_StreamingClient([response])).open_stream(
            request=_stream_request()
        ) as session:
            list(session)

    assert raised.value.code == ErrorCode.MODEL_INVALID_RESPONSE
    assert raised.value.metadata["reason"] == "model_tool_calls_unsupported"
    assert "private_tool" not in raised.value.internal_message


@pytest.mark.parametrize(
    ("lines", "reason"),
    [
        (_data("[DONE]"), "model_finish_reason_missing"),
        (
            [*_data(_chunk(content="answer", finish_reason="stop"))],
            "model_stream_incomplete",
        ),
        (
            [*_data(_chunk(content="   ", finish_reason="stop")), *_data("[DONE]")],
            "model_response_blank",
        ),
        (
            [*_data(_chunk(content="answer", index=1))],
            "model_stream_chunk_invalid",
        ),
    ],
)
def test_deepseek_stream_fails_closed_for_incomplete_or_blank_output(
    lines: list[str],
    reason: str,
) -> None:
    response = _StreamResponse(lines=lines)

    with pytest.raises(AemeathError) as raised:
        with _deepseek(_StreamingClient([response])).open_stream(
            request=_stream_request()
        ) as session:
            list(session)

    assert raised.value.code == ErrorCode.MODEL_INVALID_RESPONSE
    assert raised.value.metadata["reason"] == reason
    assert response.close_count == 1


def test_deepseek_stream_does_not_retry_after_200_or_leak_raw_chunk() -> None:
    secret = "RAW_PROVIDER_SECRET_71b2"
    response = _StreamResponse(lines=_data({"secret": secret}))
    fake = _StreamingClient([response])

    with pytest.raises(AemeathError) as raised:
        with _deepseek(fake, max_retries=1).open_stream(
            request=_stream_request()
        ) as session:
            list(session)

    assert len(fake.calls) == 1
    assert secret not in raised.value.internal_message
    assert secret not in str(raised.value.metadata)


def test_deepseek_stream_retries_connect_failure_before_response() -> None:
    response = _StreamResponse(
        lines=[
            *_data(_chunk(content="answer", finish_reason="stop")),
            *_data("[DONE]"),
        ]
    )
    fake = _StreamingClient(
        [httpx.ConnectError("connect", request=_request()), response]
    )

    with _deepseek(fake, max_retries=1).open_stream(
        request=_stream_request()
    ) as session:
        events = list(session)

    assert events[-1] == ModelStreamFinished("complete")
    assert len(fake.calls) == 2
    assert response.close_count == 1


def test_deepseek_stream_maps_read_timeout_and_closes_response() -> None:
    response = _StreamResponse(
        lines=_data(_chunk(content="partial")),
        line_error=httpx.ReadTimeout("read", request=_request()),
    )

    with pytest.raises(AemeathError) as raised:
        with _deepseek(_StreamingClient([response]), max_retries=1).open_stream(
            request=_stream_request()
        ) as session:
            list(session)

    assert raised.value.code == ErrorCode.MODEL_TIMEOUT
    assert raised.value.metadata["attempt_count"] == 1
    assert response.close_count == 1


def test_deepseek_stream_sanitizes_unexpected_read_failure() -> None:
    secret = "RAW_READ_FAILURE_SECRET_99d1"
    response = _StreamResponse(line_error=RuntimeError(secret))

    with pytest.raises(AemeathError) as raised:
        with _deepseek(_StreamingClient([response])).open_stream(
            request=_stream_request()
        ) as session:
            list(session)

    assert raised.value.code == ErrorCode.MODEL_UNAVAILABLE
    assert raised.value.metadata["reason"] == "model_stream_read_error"
    assert secret not in raised.value.internal_message
    assert secret not in str(raised.value.metadata)
    assert response.close_count == 1


def test_mock_streaming_client_is_configurable_and_closes_once() -> None:
    client = MockStreamingChatModelClient(
        events=(ModelTextDelta("a"), ModelStreamFinished("complete"))
    )
    request = _stream_request()

    with client.open_stream(request=request) as session:
        assert list(session) == [
            ModelTextDelta("a"),
            ModelStreamFinished("complete"),
        ]
        session.close()

    assert client.open_count == 1
    assert client.last_request is request
    assert client.last_session is not None
    assert client.last_session.close_count == 1


def test_mock_streaming_client_supports_partial_failure() -> None:
    failure = RuntimeError("safe_mock_failure")
    client = MockStreamingChatModelClient(
        events=(ModelTextDelta("a"), ModelTextDelta("b")),
        failure=failure,
        fail_after_events=1,
    )

    with pytest.raises(RuntimeError, match="safe_mock_failure"):
        with client.open_stream(request=_stream_request()) as session:
            list(session)

    assert client.last_session is not None
    assert client.last_session.close_count == 1
