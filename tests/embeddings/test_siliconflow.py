from __future__ import annotations

import json

import httpx
import numpy as np
import pytest

from app.embeddings.errors import (
    EmbeddingAuthError,
    EmbeddingInvalidResponseError,
    EmbeddingRateLimitError,
    EmbeddingTimeoutError,
)
from app.embeddings.providers.siliconflow import (
    SILICONFLOW_EMBEDDINGS_URL,
    SILICONFLOW_MODEL_ID,
    SiliconFlowEmbeddingProvider,
)
from app.embeddings.schemas import (
    EmbeddingRequest,
    EmbeddingRuntimeOptions,
    EmbeddingSpaceProfile,
)


def _profile() -> EmbeddingSpaceProfile:
    """A synthetic review profile; provider contract tests never promote it."""
    return EmbeddingSpaceProfile(
        schema_version="embedding-space-profile/v1",
        release_status="review",
        profile_id="siliconflow_contract_test_v1",
        provider="siliconflow",
        model_id=SILICONFLOW_MODEL_ID,
        dimension=1024,
        model_revision="provider_managed",
        pooling="provider_managed",
        normalization="l2",
        query_instruction="",
        document_instruction="",
        preprocessing_version="aemeath-embedding-pre-v1",
        postprocessing_version="normalized-float32-v1",
        canary_set_version="contract-test-canary-v1",
        canary_artifact="canaries/unused.json",
        canary_artifact_sha256="sha256:" + ("0" * 64),
        baseline_artifact=None,
        baseline_artifact_sha256=None,
        fingerprint_algorithm="baseline-relative-mask-v1",
        expected_fingerprint=None,
        qualification_thresholds=None,
    )


def _vector(coordinate: int, magnitude: float = 1.0) -> list[float]:
    result = [0.0] * 1024
    result[coordinate] = magnitude
    return result


def _success_response(
    request: httpx.Request,
    *,
    data: list[dict[str, object]] | None = None,
    usage: object = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    payload: dict[str, object] = {
        "object": "list",
        "model": SILICONFLOW_MODEL_ID,
        "data": data
        if data is not None
        else [{"index": 0, "embedding": _vector(0)}],
    }
    if usage is not None:
        payload["usage"] = usage
    # Raw JSON lets this contract test deliver a non-standard NaN token and verify
    # that the provider still rejects non-finite coordinates defensively.
    return httpx.Response(
        200,
        content=json.dumps(payload, allow_nan=True).encode("utf-8"),
        headers=headers,
        request=request,
    )


def _provider(
    handler,
    *,
    batch_size: int = 16,
    max_retries: int = 0,
    retry_backoff_seconds: float = 0.0,
    sleeper=lambda _delay: None,
    api_key: str = "sf-test-key",
) -> SiliconFlowEmbeddingProvider:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return SiliconFlowEmbeddingProvider(
        profile=_profile(),
        runtime=EmbeddingRuntimeOptions(
            batch_size=batch_size,
            timeout_seconds=3.5,
            max_retries=max_retries,
            retry_backoff_seconds=retry_backoff_seconds,
        ),
        api_key=api_key,
        client=client,
        sleeper=sleeper,
    )


def test_siliconflow_fixed_batch_contract_restores_index_order_and_normalizes() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        payload = json.loads(request.content.decode("utf-8"))
        assert str(request.url) == SILICONFLOW_EMBEDDINGS_URL
        assert payload == {
            "model": SILICONFLOW_MODEL_ID,
            "input": ["first", "second"],
            "encoding_format": "float",
        }
        assert "dimensions" not in payload
        assert request.headers["authorization"] == "Bearer sf-test-key"
        return _success_response(
            request,
            data=[
                {"index": 1, "embedding": _vector(1, 7.0)},
                {"index": 0, "embedding": _vector(0, 3.0)},
            ],
            usage={"prompt_tokens": 13, "total_tokens": 13},
            headers={"x-siliconcloud-trace-id": "trace.contract-1"},
        )

    result = _provider(handler).embed(
        EmbeddingRequest(texts=("first", "second"), purpose="query")
    )

    assert len(captured) == 1
    assert result.vectors.shape == (2, 1024)
    assert result.vectors.dtype == np.dtype(np.float32)
    assert result.vectors.flags.c_contiguous
    np.testing.assert_allclose(result.vectors[:, :2], [[1.0, 0.0], [0.0, 1.0]])
    np.testing.assert_allclose(np.linalg.norm(result.vectors, axis=1), [1.0, 1.0])
    assert result.usage is not None
    assert result.usage.input_count == 2
    assert result.usage.input_tokens == 13
    assert result.diagnostics is not None
    assert result.diagnostics.retry_count == 0
    assert result.diagnostics.provider_request_id == "trace.contract-1"
    assert result.diagnostics.provider_request_ids == ("trace.contract-1",)


def test_siliconflow_empty_batch_is_lazy() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise AssertionError("empty batches must not reach the transport")

    provider = _provider(handler, batch_size=2)
    empty = provider.embed(EmbeddingRequest(texts=(), purpose="document"))
    assert empty.vectors.shape == (0, 1024)
    assert empty.vectors.dtype == np.dtype(np.float32)
    assert empty.usage is not None and empty.usage.input_count == 0
    assert empty.diagnostics is not None
    assert empty.diagnostics.provider_request_ids == ()
    assert calls == 0


def test_siliconflow_chunks_large_logical_batch_and_restores_global_order() -> None:
    texts = tuple(f"text-{index}" for index in range(5))
    captured_inputs: list[tuple[str, ...]] = []
    token_counts = (3, 5, 7)

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        chunk_texts = tuple(payload["input"])
        captured_inputs.append(chunk_texts)
        chunk_number = len(captured_inputs)
        data = [
            {
                "index": local_index,
                "embedding": _vector(int(text.rsplit("-", 1)[-1]), 2.0),
            }
            for local_index, text in enumerate(chunk_texts)
        ]
        return _success_response(
            request,
            data=list(reversed(data)),
            usage={"prompt_tokens": token_counts[chunk_number - 1]},
            headers={"x-request-id": f"trace-chunk-{chunk_number}"},
        )

    result = _provider(handler, batch_size=2).embed(
        EmbeddingRequest(texts=texts, purpose="document")
    )

    assert captured_inputs == [
        ("text-0", "text-1"),
        ("text-2", "text-3"),
        ("text-4",),
    ]
    assert result.vectors.shape == (5, 1024)
    assert result.vectors.dtype == np.dtype("<f4")
    assert result.vectors.flags.c_contiguous
    np.testing.assert_array_equal(result.vectors[:, :5], np.eye(5, dtype=np.float32))
    assert result.usage is not None
    assert result.usage.input_count == 5
    assert result.usage.input_tokens == sum(token_counts)
    assert result.diagnostics is not None
    assert result.diagnostics.retry_count == 0
    assert result.diagnostics.provider_request_id is None
    assert result.diagnostics.provider_request_ids == (
        "trace-chunk-1",
        "trace-chunk-2",
        "trace-chunk-3",
    )


def test_siliconflow_marks_aggregate_usage_unknown_when_any_chunk_omits_it() -> None:
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        payload = json.loads(request.content.decode("utf-8"))
        chunk_size = len(payload["input"])
        data = [
            {"index": index, "embedding": _vector(index + (call_count - 1) * 2)}
            for index in range(chunk_size)
        ]
        if call_count == 1:
            return _success_response(
                request,
                data=data,
                usage={"prompt_tokens": 11},
                headers={"request-id": "trace-first"},
            )
        return _success_response(request, data=data)

    result = _provider(handler, batch_size=2).embed(
        EmbeddingRequest(texts=("one", "two", "three"), purpose="query")
    )

    assert call_count == 2
    assert result.usage is not None
    assert result.usage.input_count == 3
    assert result.usage.input_tokens is None
    assert result.diagnostics is not None
    assert result.diagnostics.provider_request_id is None
    assert result.diagnostics.provider_request_ids == ("trace-first", None)


def test_siliconflow_aggregates_retries_across_chunks_without_replaying_completed() -> None:
    attempts: dict[str, int] = {}
    call_order: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        text = payload["input"][0]
        call_order.append(text)
        attempts[text] = attempts.get(text, 0) + 1
        if text == "one" and attempts[text] == 1:
            return httpx.Response(429, request=request)
        if text == "two" and attempts[text] == 1:
            raise httpx.ReadTimeout("synthetic timeout", request=request)
        coordinate = {"one": 0, "two": 1, "three": 2}[text]
        return _success_response(
            request,
            data=[{"index": 0, "embedding": _vector(coordinate)}],
            usage={"prompt_tokens": coordinate + 1},
            headers={"x-request-id": f"trace-{text}"},
        )

    result = _provider(handler, batch_size=1, max_retries=1).embed(
        EmbeddingRequest(texts=("one", "two", "three"), purpose="query")
    )

    assert call_order == ["one", "one", "two", "two", "three"]
    np.testing.assert_array_equal(result.vectors[:, :3], np.eye(3, dtype=np.float32))
    assert result.usage is not None and result.usage.input_tokens == 6
    assert result.diagnostics is not None
    assert result.diagnostics.retry_count == 2
    assert result.diagnostics.provider_request_id is None
    assert result.diagnostics.provider_request_ids == (
        "trace-one",
        "trace-two",
        "trace-three",
    )


def test_siliconflow_later_chunk_failure_accumulates_retries_and_stops() -> None:
    attempts: dict[str, int] = {}
    call_order: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        text = payload["input"][0]
        call_order.append(text)
        attempts[text] = attempts.get(text, 0) + 1
        if text == "first" and attempts[text] == 1:
            return httpx.Response(503, request=request)
        if text == "first":
            return _success_response(
                request,
                headers={"x-request-id": "trace-first-success"},
            )
        if text == "second":
            return httpx.Response(
                429,
                headers={"x-request-id": f"trace-second-{attempts[text]}"},
                request=request,
            )
        raise AssertionError("chunks after a terminal failure must not be sent")

    with pytest.raises(EmbeddingRateLimitError) as raised:
        _provider(handler, batch_size=1, max_retries=1).embed(
            EmbeddingRequest(texts=("first", "second", "never"), purpose="document")
        )

    assert call_order == ["first", "first", "second", "second"]
    assert raised.value.retry_count == 2
    assert raised.value.metadata["retry_count"] == 2
    assert raised.value.provider_request_id == "trace-second-2"


@pytest.mark.parametrize(
    ("data", "texts", "reason"),
    [
        ([{"index": 0, "embedding": _vector(0)}], ("one", "two"), "response_data_count"),
        (
            [
                {"index": 0, "embedding": _vector(0)},
                {"index": 0, "embedding": _vector(1)},
            ],
            ("one", "two"),
            "response_index_invalid",
        ),
        ([{"embedding": _vector(0)}], ("one",), "response_index_invalid"),
        ([{"index": 0, "embedding": [1.0] * 1023}], ("one",), "remote_output_shape"),
        ([{"index": 0, "embedding": [float("nan")] * 1024}], ("one",), "remote_output_non_finite"),
        ([{"index": 0, "embedding": [0.0] * 1024}], ("one",), "remote_output_zero_norm"),
    ],
)
def test_siliconflow_rejects_incomplete_or_corrupt_batches(
    data: list[dict[str, object]],
    texts: tuple[str, ...],
    reason: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _success_response(request, data=data)

    with pytest.raises(EmbeddingInvalidResponseError) as raised:
        _provider(handler).embed(EmbeddingRequest(texts=texts, purpose="document"))
    assert raised.value.reason == reason


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"x-request-id": "request_123"}, "request_123"),
        ({"request-id": "request/456"}, "request/456"),
        ({"x-provider-internal-id": "not-allowlisted"}, None),
        ({"x-request-id": "x" * 129}, None),
    ],
)
def test_siliconflow_request_id_uses_a_strict_allowlist(
    headers: dict[str, str],
    expected: str | None,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _success_response(request, headers=headers)

    result = _provider(handler).embed(EmbeddingRequest(texts=("one",), purpose="query"))
    assert result.diagnostics is not None
    assert result.diagnostics.provider_request_id == expected


@pytest.mark.parametrize("transient_status", [429, 500, 503])
def test_siliconflow_retries_retryable_statuses(transient_status: int) -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                transient_status,
                headers={"retry-after": "0.125"},
                request=request,
            )
        return _success_response(request)

    result = _provider(
        handler,
        max_retries=1,
        retry_backoff_seconds=0.01,
        sleeper=sleeps.append,
    ).embed(EmbeddingRequest(texts=("retry me",), purpose="query"))

    assert calls == 2
    assert sleeps == [0.125]
    assert result.diagnostics is not None and result.diagnostics.retry_count == 1


def test_siliconflow_retries_timeout_then_reports_retry_count() -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadTimeout("synthetic timeout", request=request)
        return _success_response(request)

    result = _provider(
        handler,
        max_retries=1,
        retry_backoff_seconds=0.25,
        sleeper=sleeps.append,
    ).embed(EmbeddingRequest(texts=("retry me",), purpose="query"))

    assert calls == 2
    assert sleeps == [0.25]
    assert result.diagnostics is not None and result.diagnostics.retry_count == 1


def test_siliconflow_exhausted_timeout_is_typed() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("synthetic timeout", request=request)

    with pytest.raises(EmbeddingTimeoutError) as raised:
        _provider(handler, max_retries=1).embed(
            EmbeddingRequest(texts=("timeout",), purpose="query")
        )
    assert calls == 2
    assert raised.value.retry_count == 1


def test_siliconflow_auth_failure_is_not_retried_and_secrets_are_not_rendered() -> None:
    calls = 0
    api_key = "sf-secret-must-not-leak"
    upstream_body = "sensitive-upstream-response-body"

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401, text=upstream_body, request=request)

    provider = _provider(handler, max_retries=3, api_key=api_key)
    with pytest.raises(EmbeddingAuthError) as raised:
        provider.embed(EmbeddingRequest(texts=("private input",), purpose="document"))

    assert calls == 1
    rendered = " ".join(
        (repr(provider), str(raised.value), repr(raised.value), repr(raised.value.metadata))
    )
    assert api_key not in rendered
    assert upstream_body not in rendered
    assert "private input" not in rendered
