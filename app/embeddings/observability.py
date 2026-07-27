from __future__ import annotations

import re
import time
from typing import Callable

from app.embeddings.compat import LegacyEmbeddingMethodsMixin
from app.embeddings.errors import EmbeddingError
from app.embeddings.ports import EmbeddingProviderPort
from app.embeddings.schemas import EmbeddingBatchResult, EmbeddingModelSignature, EmbeddingRequest
from app.errors import map_exception
from app.errors.codes import ErrorCode
from app.observability.noop import NoopAppLogger
from app.observability.ports import AppLoggerPort
from app.observability.schemas import AppLogRecord

_SAFE_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:/-]{1,128}$")


def record_qualification_terminal(
    app_logger: AppLoggerPort,
    *,
    passed: bool,
    command: str,
    provider: str | None,
    profile_id: str | None,
    release_status: str | None,
    dimension: int | None,
    canary_count: int | None,
    observation_count: int,
    signature_fingerprint: str | None,
    duration_ms: float,
    error_code: str | None,
) -> None:
    """Write one allowlisted qualification terminal event without changing outcomes."""
    record = AppLogRecord(
        event_name=(
            "embedding.qualification_completed"
            if passed
            else "embedding.qualification_failed"
        ),
        level="info" if passed else "error",
        payload={
            "command": command,
            "provider": provider,
            "profile_id": profile_id,
            "release_status": release_status,
            "dimension": dimension,
            "canary_count": canary_count,
            "observation_count": observation_count,
            "signature_fingerprint": signature_fingerprint,
            "duration_ms": round(max(0.0, duration_ms), 3),
        },
        error_code=error_code,
    )
    try:
        app_logger.record(record)
    except Exception:
        return None


class ObservableEmbeddingProvider(LegacyEmbeddingMethodsMixin):
    """Record one redacted terminal event for each logical embedding batch."""

    def __init__(
        self,
        provider: EmbeddingProviderPort,
        *,
        app_logger: AppLoggerPort | None = None,
        clock: Callable[[], float] = time.perf_counter,
    ):
        self._provider = provider
        self._app_logger = app_logger or NoopAppLogger()
        self._clock = clock
        legacy_name = getattr(provider, "model_name", provider.signature.model_id)
        self._legacy_model_name = (
            legacy_name if isinstance(legacy_name, str) else provider.signature.model_id
        )

    @property
    def signature(self) -> EmbeddingModelSignature:
        return self._provider.signature

    def embed(self, request: EmbeddingRequest) -> EmbeddingBatchResult:
        started_at = self._clock()
        try:
            result = self._provider.embed(request)
        except Exception as exc:
            duration_ms = max(0.0, (self._clock() - started_at) * 1000.0)
            retry_count = exc.retry_count if isinstance(exc, EmbeddingError) else 0
            request_id = exc.provider_request_id if isinstance(exc, EmbeddingError) else None
            try:
                error_code = map_exception(exc).code.value
            except Exception:
                error_code = ErrorCode.INTERNAL_ERROR.value
            self._record_safely(
                AppLogRecord(
                    event_name="embedding.batch_failed",
                    level="error",
                    payload=self._payload(
                        request=request,
                        input_tokens=None,
                        duration_ms=duration_ms,
                        retry_count=retry_count,
                        provider_request_id=request_id,
                    ),
                    error_code=error_code,
                )
            )
            raise
        duration_ms = max(0.0, (self._clock() - started_at) * 1000.0)
        diagnostics = result.diagnostics
        usage = result.usage
        self._record_safely(
            AppLogRecord(
                event_name="embedding.batch_completed",
                payload=self._payload(
                    request=request,
                    input_tokens=usage.input_tokens if usage is not None else None,
                    duration_ms=duration_ms,
                    retry_count=diagnostics.retry_count if diagnostics is not None else 0,
                    provider_request_id=(
                        diagnostics.provider_request_id if diagnostics is not None else None
                    ),
                ),
            )
        )
        return result

    def _payload(
        self,
        *,
        request: EmbeddingRequest,
        input_tokens: int | None,
        duration_ms: float,
        retry_count: int,
        provider_request_id: str | None,
    ) -> dict[str, object]:
        safe_request_id = (
            provider_request_id
            if isinstance(provider_request_id, str)
            and _SAFE_REQUEST_ID_RE.fullmatch(provider_request_id)
            else None
        )
        purpose = request.purpose if request.purpose in {"query", "document"} else "invalid"
        batch_size = len(request.texts) if isinstance(request.texts, tuple) else 0
        return {
            "provider": self.signature.provider,
            "profile_id": self.signature.profile_id,
            "purpose": purpose,
            "batch_size": batch_size,
            "input_tokens": input_tokens,
            "duration_ms": round(duration_ms, 3),
            "retry_count": retry_count,
            "provider_request_id": safe_request_id,
        }

    def _record_safely(self, record: AppLogRecord) -> None:
        try:
            self._app_logger.record(record)
        except Exception:
            # Logging must never alter embedding success/failure semantics.
            return None
