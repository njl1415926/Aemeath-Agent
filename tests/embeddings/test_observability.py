from __future__ import annotations

import json
from dataclasses import asdict

import numpy as np
import pytest

from app.embeddings.errors import EmbeddingUnavailableError
from app.embeddings.observability import ObservableEmbeddingProvider
from app.embeddings.providers.hash import DeterministicHashEmbeddingProvider
from app.embeddings.schemas import (
    EmbeddingBatchResult,
    EmbeddingCallDiagnostics,
    EmbeddingRequest,
    EmbeddingUsage,
)
from app.observability.schemas import AppLogRecord

SENTINEL = "NEVER_LOG_THIS_EMBEDDING_SECRET"
EXPECTED_PAYLOAD_FIELDS = {
    "provider",
    "profile_id",
    "purpose",
    "batch_size",
    "input_tokens",
    "duration_ms",
    "retry_count",
    "provider_request_id",
}


class _CollectingLogger:
    def __init__(self) -> None:
        self.records: list[AppLogRecord] = []

    def record(self, record: AppLogRecord) -> None:
        self.records.append(record)


class _SuccessProvider:
    def __init__(self) -> None:
        self.signature = DeterministicHashEmbeddingProvider(dimension=2).signature
        self.result = EmbeddingBatchResult(
            vectors=np.asarray([[1.0, 0.0]], dtype=np.float32),
            signature=self.signature,
            usage=EmbeddingUsage(input_count=1, input_tokens=17),
            diagnostics=EmbeddingCallDiagnostics(
                retry_count=2,
                provider_request_id="trace/Safe_123",
            ),
        )

    def embed(self, request: EmbeddingRequest) -> EmbeddingBatchResult:
        assert request.texts == (SENTINEL,)
        return self.result


class _FailureProvider:
    def __init__(self) -> None:
        self.signature = DeterministicHashEmbeddingProvider(dimension=2).signature
        self.error = EmbeddingUnavailableError(
            reason=f"upstream_{SENTINEL}",
            retry_count=3,
            provider_request_id=f"unsafe request id {SENTINEL}",
        )

    def embed(self, request: EmbeddingRequest) -> EmbeddingBatchResult:
        raise self.error


def _serialized(record: AppLogRecord) -> str:
    return json.dumps(asdict(record), ensure_ascii=False, sort_keys=True)


def test_observable_provider_records_only_allowlisted_success_metadata() -> None:
    provider = _SuccessProvider()
    logger = _CollectingLogger()
    ticks = iter((10.0, 10.123456))
    observable = ObservableEmbeddingProvider(
        provider,
        app_logger=logger,
        clock=lambda: next(ticks),
    )
    request = EmbeddingRequest(texts=(SENTINEL,), purpose="query")

    result = observable.embed(request)

    assert result is provider.result
    assert len(logger.records) == 1
    record = logger.records[0]
    assert record.event_name == "embedding.batch_completed"
    assert record.level == "info"
    assert record.error_code is None
    assert set(record.payload) == EXPECTED_PAYLOAD_FIELDS
    assert record.payload == {
        "provider": "deterministic_hash",
        "profile_id": "deterministic_hash_v1_dim_2",
        "purpose": "query",
        "batch_size": 1,
        "input_tokens": 17,
        "duration_ms": pytest.approx(123.456),
        "retry_count": 2,
        "provider_request_id": "trace/Safe_123",
    }
    raw = _serialized(record)
    assert SENTINEL not in raw
    assert "vectors" not in raw
    assert "api_key" not in raw


def test_observable_provider_records_redacted_failure_and_reraises_identity() -> None:
    provider = _FailureProvider()
    logger = _CollectingLogger()
    ticks = iter((20.0, 20.25))
    observable = ObservableEmbeddingProvider(
        provider,
        app_logger=logger,
        clock=lambda: next(ticks),
    )
    request = EmbeddingRequest(texts=(SENTINEL,), purpose="document")

    with pytest.raises(EmbeddingUnavailableError) as exc_info:
        observable.embed(request)

    assert exc_info.value is provider.error
    assert len(logger.records) == 1
    record = logger.records[0]
    assert record.event_name == "embedding.batch_failed"
    assert record.level == "error"
    assert record.error_code == "EMBEDDING_UNAVAILABLE"
    assert set(record.payload) == EXPECTED_PAYLOAD_FIELDS
    assert record.payload == {
        "provider": "deterministic_hash",
        "profile_id": "deterministic_hash_v1_dim_2",
        "purpose": "document",
        "batch_size": 1,
        "input_tokens": None,
        "duration_ms": 250.0,
        "retry_count": 3,
        "provider_request_id": None,
    }
    raw = _serialized(record)
    assert SENTINEL not in raw
    assert "vectors" not in raw
    assert "api_key" not in raw


def test_observability_failure_never_changes_embedding_success_semantics() -> None:
    class _FailingLogger:
        def record(self, record: AppLogRecord) -> None:
            raise RuntimeError(f"logger failure {SENTINEL}")

    provider = _SuccessProvider()
    ticks = iter((1.0, 1.1))
    observable = ObservableEmbeddingProvider(
        provider,
        app_logger=_FailingLogger(),
        clock=lambda: next(ticks),
    )

    result = observable.embed(EmbeddingRequest(texts=(SENTINEL,), purpose="query"))

    assert result is provider.result
