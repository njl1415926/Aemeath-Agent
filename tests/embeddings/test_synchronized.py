from __future__ import annotations

from dataclasses import dataclass
from threading import Event, Lock, Thread

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
from app.embeddings.synchronized import SynchronizedEmbeddingProvider
from app.observability.schemas import AppLogRecord


def _result() -> EmbeddingBatchResult:
    signature = DeterministicHashEmbeddingProvider(dimension=2).signature
    return EmbeddingBatchResult(
        vectors=np.asarray([[1.0, 0.0]], dtype=np.float32),
        signature=signature,
        usage=EmbeddingUsage(input_count=1, input_tokens=7),
        diagnostics=EmbeddingCallDiagnostics(
            retry_count=3,
            provider_request_id="request-safe",
            provider_request_ids=("request-safe",),
        ),
    )


class _IdentityDelegate:
    def __init__(self) -> None:
        self.result = _result()
        self.signature = self.result.signature

    def embed(self, request: EmbeddingRequest) -> EmbeddingBatchResult:
        return self.result


def test_synchronized_provider_preserves_result_identity_and_diagnostics() -> None:
    delegate = _IdentityDelegate()
    provider = SynchronizedEmbeddingProvider(delegate)

    result = provider.embed(EmbeddingRequest(texts=("query",), purpose="query"))

    assert result is delegate.result
    assert result.vectors is delegate.result.vectors
    assert result.usage is delegate.result.usage
    assert result.diagnostics is delegate.result.diagnostics
    assert provider.signature is delegate.signature


def test_synchronized_provider_serializes_concurrent_first_lazy_load() -> None:
    first_entered = Event()
    release_first = Event()
    second_entered = Event()
    second_waiting_for_lock = Event()

    class _SignalingLock:
        def __init__(self) -> None:
            self._inner = Lock()
            self._attempt_lock = Lock()
            self._attempt_count = 0

        def __enter__(self):
            with self._attempt_lock:
                self._attempt_count += 1
                if self._attempt_count == 2:
                    second_waiting_for_lock.set()
            self._inner.acquire()
            return self

        def __exit__(self, exc_type, exc_value, traceback) -> None:
            self._inner.release()

    class _BlockingDelegate:
        def __init__(self) -> None:
            self.result = _result()
            self.signature = self.result.signature
            self._state_lock = Lock()
            self.active = 0
            self.maximum_active = 0
            self.call_count = 0
            self.load_count = 0
            self.loaded = False

        def embed(self, request: EmbeddingRequest) -> EmbeddingBatchResult:
            with self._state_lock:
                self.call_count += 1
                self.active += 1
                self.maximum_active = max(self.maximum_active, self.active)
            try:
                if not self.loaded:
                    self.load_count += 1
                    first_entered.set()
                    assert release_first.wait(timeout=2.0)
                    self.loaded = True
                else:
                    second_entered.set()
                return self.result
            finally:
                with self._state_lock:
                    self.active -= 1

    delegate = _BlockingDelegate()
    provider = SynchronizedEmbeddingProvider(delegate)
    provider._lock = _SignalingLock()
    results: list[EmbeddingBatchResult] = []

    def call_first() -> None:
        results.append(provider.embed(EmbeddingRequest(texts=("first",), purpose="query")))

    def call_second() -> None:
        results.append(provider.embed(EmbeddingRequest(texts=("second",), purpose="document")))

    first = Thread(target=call_first)
    second = Thread(target=call_second)
    first.start()
    assert first_entered.wait(timeout=2.0)
    second.start()
    assert second_waiting_for_lock.wait(timeout=2.0)
    assert not second_entered.is_set()
    release_first.set()
    assert second_entered.wait(timeout=2.0)
    first.join(timeout=2.0)
    second.join(timeout=2.0)

    assert not first.is_alive()
    assert not second.is_alive()
    assert delegate.maximum_active == 1
    assert delegate.load_count == 1
    assert results == [delegate.result, delegate.result]


def test_synchronized_provider_reraises_identity_and_releases_lock() -> None:
    error = EmbeddingUnavailableError(
        reason="test_unavailable",
        retry_count=4,
        provider_request_id="request-safe",
    )

    class _FailThenSucceedDelegate(_IdentityDelegate):
        def __init__(self) -> None:
            super().__init__()
            self.call_count = 0

        def embed(self, request: EmbeddingRequest) -> EmbeddingBatchResult:
            self.call_count += 1
            if self.call_count == 1:
                raise error
            return self.result

    delegate = _FailThenSucceedDelegate()
    provider = SynchronizedEmbeddingProvider(delegate)

    with pytest.raises(EmbeddingUnavailableError) as raised:
        provider.embed(EmbeddingRequest(texts=("first",), purpose="query"))
    completed = Event()
    results: list[EmbeddingBatchResult] = []

    def call_after_failure() -> None:
        results.append(
            provider.embed(EmbeddingRequest(texts=("second",), purpose="query"))
        )
        completed.set()

    worker = Thread(target=call_after_failure)
    worker.start()
    assert completed.wait(timeout=2.0)
    worker.join(timeout=2.0)

    assert raised.value is error
    assert raised.value.retry_count == 4
    assert raised.value.provider_request_id == "request-safe"
    assert not worker.is_alive()
    assert results == [delegate.result]


@dataclass
class _CollectingLogger:
    records: list[AppLogRecord]

    def record(self, record: AppLogRecord) -> None:
        self.records.append(record)


def test_synchronized_outer_wrapper_does_not_duplicate_terminal_events() -> None:
    delegate = _IdentityDelegate()
    logger = _CollectingLogger(records=[])
    ticks = iter((10.0, 10.25))
    observable = ObservableEmbeddingProvider(
        delegate,
        app_logger=logger,
        clock=lambda: next(ticks),
    )
    provider = SynchronizedEmbeddingProvider(observable)

    result = provider.embed(EmbeddingRequest(texts=("query",), purpose="query"))

    assert result is delegate.result
    assert len(logger.records) == 1
    assert logger.records[0].event_name == "embedding.batch_completed"
    assert logger.records[0].payload["duration_ms"] == 250.0


def test_synchronized_outer_wrapper_preserves_one_failed_terminal_event() -> None:
    error = EmbeddingUnavailableError(
        reason="test_unavailable",
        retry_count=5,
        provider_request_id="request-safe",
    )

    class _FailureDelegate(_IdentityDelegate):
        def embed(self, request: EmbeddingRequest) -> EmbeddingBatchResult:
            raise error

    delegate = _FailureDelegate()
    logger = _CollectingLogger(records=[])
    ticks = iter((20.0, 20.5))
    provider = SynchronizedEmbeddingProvider(
        ObservableEmbeddingProvider(
            delegate,
            app_logger=logger,
            clock=lambda: next(ticks),
        )
    )

    with pytest.raises(EmbeddingUnavailableError) as raised:
        provider.embed(EmbeddingRequest(texts=("query",), purpose="query"))

    assert raised.value is error
    assert len(logger.records) == 1
    assert logger.records[0].event_name == "embedding.batch_failed"
    assert logger.records[0].payload["retry_count"] == 5
    assert logger.records[0].payload["provider_request_id"] == "request-safe"


def test_foundation_duration_starts_after_outer_lock_wait() -> None:
    lock_attempted = Event()
    release_lock = Event()
    clock_calls: list[float] = []

    class _BlockingLock:
        def __enter__(self):
            lock_attempted.set()
            assert release_lock.wait(timeout=2.0)
            return self

        def __exit__(self, exc_type, exc_value, traceback) -> None:
            return None

    delegate = _IdentityDelegate()
    logger = _CollectingLogger(records=[])
    ticks = iter((30.0, 30.125))

    def clock() -> float:
        value = next(ticks)
        clock_calls.append(value)
        return value

    provider = SynchronizedEmbeddingProvider(
        ObservableEmbeddingProvider(delegate, app_logger=logger, clock=clock)
    )
    provider._lock = _BlockingLock()
    results: list[EmbeddingBatchResult] = []
    worker = Thread(
        target=lambda: results.append(
            provider.embed(EmbeddingRequest(texts=("query",), purpose="query"))
        )
    )

    worker.start()
    assert lock_attempted.wait(timeout=2.0)
    assert clock_calls == []
    release_lock.set()
    worker.join(timeout=2.0)

    assert not worker.is_alive()
    assert results == [delegate.result]
    assert clock_calls == [30.0, 30.125]
    assert logger.records[0].payload["duration_ms"] == 125.0
