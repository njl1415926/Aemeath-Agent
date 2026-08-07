from __future__ import annotations

from contextlib import contextmanager
from threading import BoundedSemaphore, Condition, Lock, get_ident
from time import monotonic
from typing import Iterator

from app.embeddings.compat import LegacyEmbeddingMethodsMixin
from app.embeddings.ports import EmbeddingProviderPort
from app.embeddings.schemas import EmbeddingBatchResult, EmbeddingModelSignature, EmbeddingRequest
from app.errors import map_exception, redact_sensitive
from app.model.streaming import ModelStreamRequest, ModelStreamSession
from app.observability.noop import NoopAppLogger
from app.observability.ports import AppLoggerPort
from app.observability.schemas import AppLogRecord
from app.runtime.guardrails.errors import RuntimeBusyError


class ConcurrencyGate:
    def __init__(self, *, limit: int, wait_timeout_seconds: float, reason: str):
        if limit <= 0 or wait_timeout_seconds < 0:
            raise ValueError("invalid concurrency gate configuration")
        self.limit = limit
        self.wait_timeout_seconds = wait_timeout_seconds
        self.reason = reason
        self._semaphore = BoundedSemaphore(limit)
        self._stats_lock = Lock()
        self._active_count = 0
        self._peak_active_count = 0

    @contextmanager
    def acquire(self) -> Iterator[float]:
        handle = self.open_handle()
        try:
            yield handle.wait_ms
        finally:
            handle.close()

    def open_handle(self) -> ConcurrencyPermitHandle:
        started = monotonic()
        acquired = self._semaphore.acquire(timeout=self.wait_timeout_seconds)
        waited_ms = max(0.0, (monotonic() - started) * 1000.0)
        if not acquired:
            raise RuntimeBusyError(self.reason)
        with self._stats_lock:
            self._active_count += 1
            self._peak_active_count = max(self._peak_active_count, self._active_count)
        return ConcurrencyPermitHandle(gate=self, wait_ms=waited_ms)

    def _release_permit(self) -> None:
        with self._stats_lock:
            self._active_count -= 1
        self._semaphore.release()

    def counts(self) -> tuple[int, int]:
        with self._stats_lock:
            return self._active_count, self._peak_active_count


class ConcurrencyPermitHandle:
    """A cross-thread, idempotently closeable concurrency permit."""

    def __init__(self, *, gate: ConcurrencyGate, wait_ms: float) -> None:
        self.wait_ms = wait_ms
        self._gate = gate
        self._condition = Condition(Lock())
        self._state = "open"
        self._closing_thread_id: int | None = None

    def close(self) -> None:
        current_thread_id = get_ident()
        with self._condition:
            if self._state == "closed":
                return
            if self._state == "closing":
                if self._closing_thread_id == current_thread_id:
                    return
                deadline = monotonic() + 5.0
                while self._state != "closed":
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        return
                    self._condition.wait(timeout=remaining)
                return
            self._state = "closing"
            self._closing_thread_id = current_thread_id
        try:
            self._gate._release_permit()
        finally:
            with self._condition:
                self._state = "closed"
                self._closing_thread_id = None
                self._condition.notify_all()


class LimitedChatModelClient:
    def __init__(
        self,
        *,
        gate: ConcurrencyGate,
        delegate,
        purpose: str = "dialogue",
        app_logger: AppLoggerPort | None = None,
    ):
        self.gate = gate
        self.delegate = delegate
        self.purpose = purpose
        self.app_logger = app_logger or NoopAppLogger()

    def chat(self, *, messages: list[dict[str, str]]) -> str:
        try:
            with self.gate.acquire() as wait_ms:
                active, peak = self.gate.counts()
                self._record("runtime.llm_admitted", "info", {
                    "purpose": self.purpose,
                    "wait_ms": round(wait_ms, 3),
                    "active_count": active,
                    "peak_active_count": peak,
                    "limit": self.gate.limit,
                })
                started = monotonic()
                try:
                    result = self.delegate.chat(messages=messages)
                except Exception as exc:
                    error = map_exception(exc)
                    self._record("runtime.llm_completed", "error", {
                        "purpose": self.purpose,
                        "status": "failed",
                        "execution_ms": round((monotonic() - started) * 1000.0, 3),
                        "error_code": error.code.value,
                    })
                    raise
                self._record("runtime.llm_completed", "info", {
                    "purpose": self.purpose,
                    "status": "completed",
                    "execution_ms": round((monotonic() - started) * 1000.0, 3),
                })
                return result
        except RuntimeBusyError:
            self._record("runtime.llm_rejected", "warning", {
                "purpose": self.purpose,
                "limit_reason": self.gate.reason,
                "limit": self.gate.limit,
            })
            raise

    def _record(self, event_name: str, level: str, payload: dict) -> None:
        self.app_logger.record(
            AppLogRecord(
                event_name=event_name,
                level=level,
                payload=redact_sensitive(payload),
            )
        )


class LimitedStreamingChatModelClient:
    """Hold the shared LLM gate for the complete upstream stream lifetime."""

    def __init__(
        self,
        *,
        gate: ConcurrencyGate,
        delegate,
        purpose: str = "dialogue_stream",
        app_logger: AppLoggerPort | None = None,
    ) -> None:
        self.gate = gate
        self.delegate = delegate
        self.purpose = purpose
        self.app_logger = app_logger or NoopAppLogger()

    @contextmanager
    def open_stream(
        self,
        *,
        request: ModelStreamRequest,
    ) -> Iterator[ModelStreamSession]:
        try:
            with self.gate.acquire() as wait_ms:
                active, peak = self.gate.counts()
                self._record(
                    "runtime.llm_admitted",
                    "info",
                    {
                        "purpose": self.purpose,
                        "wait_ms": round(wait_ms, 3),
                        "active_count": active,
                        "peak_active_count": peak,
                        "limit": self.gate.limit,
                    },
                )
                started = monotonic()
                try:
                    with self.delegate.open_stream(request=request) as stream:
                        yield stream
                except Exception as exc:
                    error = map_exception(exc)
                    self._record(
                        "runtime.llm_completed",
                        "error",
                        {
                            "purpose": self.purpose,
                            "status": "failed",
                            "execution_ms": round(
                                (monotonic() - started) * 1000.0,
                                3,
                            ),
                            "error_code": error.code.value,
                        },
                    )
                    raise
                self._record(
                    "runtime.llm_completed",
                    "info",
                    {
                        "purpose": self.purpose,
                        "status": "completed",
                        "execution_ms": round((monotonic() - started) * 1000.0, 3),
                    },
                )
        except RuntimeBusyError:
            self._record(
                "runtime.llm_rejected",
                "warning",
                {
                    "purpose": self.purpose,
                    "limit_reason": self.gate.reason,
                    "limit": self.gate.limit,
                },
            )
            raise

    def _record(self, event_name: str, level: str, payload: dict) -> None:
        self.app_logger.record(
            AppLogRecord(
                event_name=event_name,
                level=level,
                payload=redact_sensitive(payload),
            )
        )


class LimitedEmbeddingProvider(LegacyEmbeddingMethodsMixin):
    def __init__(
        self,
        *,
        gate: ConcurrencyGate,
        delegate: EmbeddingProviderPort,
        app_logger: AppLoggerPort | None = None,
    ):
        self.gate = gate
        self._provider = delegate
        self.app_logger = app_logger or NoopAppLogger()
        legacy_name = getattr(delegate, "model_name", delegate.signature.model_id)
        self._legacy_model_name = (
            legacy_name if isinstance(legacy_name, str) else delegate.signature.model_id
        )

    @property
    def signature(self) -> EmbeddingModelSignature:
        return self._provider.signature

    def embed(self, request: EmbeddingRequest) -> EmbeddingBatchResult:
        try:
            with self.gate.acquire() as wait_ms:
                active, peak = self.gate.counts()
                self.app_logger.record(
                    AppLogRecord(
                        event_name="runtime.embedding_admitted",
                        payload=redact_sensitive(
                            {
                                "purpose": request.purpose,
                                "batch_size": len(request.texts),
                                "wait_ms": round(wait_ms, 3),
                                "active_count": active,
                                "peak_active_count": peak,
                                "limit": self.gate.limit,
                            }
                        ),
                    )
                )
                return self._provider.embed(request)
        except RuntimeBusyError:
            self.app_logger.record(
                AppLogRecord(
                    event_name="runtime.embedding_rejected",
                    level="warning",
                    payload={
                        "purpose": request.purpose,
                        "batch_size": len(request.texts),
                        "limit_reason": self.gate.reason,
                        "limit": self.gate.limit,
                        "attempt_count": 0,
                    },
                )
            )
            raise
