from __future__ import annotations

import re
import time
from typing import Any, Callable

import numpy as np

from app.embeddings.errors import (
    EmbeddingAuthError,
    EmbeddingConfigurationError,
    EmbeddingDependencyError,
    EmbeddingError,
    EmbeddingInvalidResponseError,
    EmbeddingRateLimitError,
    EmbeddingRequestRejectedError,
    EmbeddingSecretMissingError,
    EmbeddingTimeoutError,
    EmbeddingUnavailableError,
)
from app.embeddings.providers.base import BaseEmbeddingProvider
from app.embeddings.schemas import (
    EmbeddingBatchResult,
    EmbeddingCallDiagnostics,
    EmbeddingRequest,
    EmbeddingRuntimeOptions,
    EmbeddingSpaceProfile,
    EmbeddingUsage,
)
from app.embeddings.validation import (
    empty_vectors,
    normalize_and_validate_vectors,
    validate_request,
)

SILICONFLOW_EMBEDDINGS_URL = "https://api.siliconflow.cn/v1/embeddings"
SILICONFLOW_MODEL_ID = "Pro/BAAI/bge-m3"
_REQUEST_ID_HEADERS = (
    "x-siliconcloud-trace-id",
    "x-request-id",
    "request-id",
)
_SAFE_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:/-]{1,128}$")


class SiliconFlowEmbeddingProvider(BaseEmbeddingProvider):
    def __init__(
        self,
        *,
        profile: EmbeddingSpaceProfile,
        runtime: EmbeddingRuntimeOptions,
        api_key: str,
        client: Any | None = None,
        client_factory: Callable[[], Any] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        if profile.provider != "siliconflow":
            raise EmbeddingConfigurationError("siliconflow_profile_provider_mismatch")
        if (
            profile.model_id != SILICONFLOW_MODEL_ID
            or profile.dimension != 1024
            or profile.model_revision != "provider_managed"
            or profile.pooling != "provider_managed"
            or profile.normalization != "l2"
            or profile.query_instruction
            or profile.document_instruction
        ):
            raise EmbeddingConfigurationError("siliconflow_profile_contract_mismatch")
        if not isinstance(api_key, str) or not api_key.strip():
            raise EmbeddingSecretMissingError()
        super().__init__(signature=profile.signature, legacy_model_name=SILICONFLOW_MODEL_ID)
        self._runtime = runtime
        self._api_key = api_key
        self._client = client
        self._client_factory = client_factory
        self._sleeper = sleeper

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(profile_id={self.signature.profile_id!r}, "
            f"batch_size={self._runtime.batch_size!r})"
        )

    def embed(self, request: EmbeddingRequest) -> EmbeddingBatchResult:
        validate_request(request)
        if not request.texts:
            return EmbeddingBatchResult(
                vectors=empty_vectors(self.dimension),
                signature=self.signature,
                usage=EmbeddingUsage(input_count=0, input_tokens=None),
                diagnostics=EmbeddingCallDiagnostics(),
            )
        httpx = self._load_httpx()
        client = self._get_client(httpx)
        chunks: list[EmbeddingBatchResult] = []
        request_ids: list[str | None] = []
        total_retries = 0
        input_tokens_total = 0
        input_tokens_complete = True
        for start in range(0, len(request.texts), self._runtime.batch_size):
            texts = request.texts[start:start + self._runtime.batch_size]
            try:
                chunk = self._embed_chunk(client=client, httpx=httpx, texts=texts)
            except EmbeddingError as exc:
                exc.add_retry_offset(total_retries)
                raise
            chunks.append(chunk)
            diagnostics = chunk.diagnostics or EmbeddingCallDiagnostics()
            total_retries += diagnostics.retry_count
            request_ids.append(diagnostics.provider_request_id)
            if chunk.usage is None or chunk.usage.input_tokens is None:
                input_tokens_complete = False
            else:
                input_tokens_total += chunk.usage.input_tokens

        combined = np.concatenate([chunk.vectors for chunk in chunks], axis=0)
        vectors = normalize_and_validate_vectors(
            combined,
            input_count=len(request.texts),
            dimension=self.dimension,
            reason_prefix="remote_output",
        )
        return EmbeddingBatchResult(
            vectors=vectors,
            signature=self.signature,
            usage=EmbeddingUsage(
                input_count=len(request.texts),
                input_tokens=input_tokens_total if input_tokens_complete else None,
            ),
            diagnostics=EmbeddingCallDiagnostics(
                retry_count=total_retries,
                provider_request_id=request_ids[0] if len(request_ids) == 1 else None,
                provider_request_ids=tuple(request_ids),
            ),
        )

    def _embed_chunk(
        self,
        *,
        client: Any,
        httpx: Any,
        texts: tuple[str, ...],
    ) -> EmbeddingBatchResult:
        payload = {
            "model": SILICONFLOW_MODEL_ID,
            "input": list(texts),
            "encoding_format": "float",
        }
        for retry_count in range(self._runtime.max_retries + 1):
            response = None
            try:
                response = client.post(
                    SILICONFLOW_EMBEDDINGS_URL,
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                    timeout=self._runtime.timeout_seconds,
                )
            except httpx.TimeoutException as exc:
                if retry_count < self._runtime.max_retries:
                    self._sleep_for_retry(retry_count, None)
                    continue
                raise EmbeddingTimeoutError(retry_count=retry_count) from exc
            except httpx.RequestError as exc:
                if retry_count < self._runtime.max_retries:
                    self._sleep_for_retry(retry_count, None)
                    continue
                raise EmbeddingUnavailableError(
                    reason="network_error",
                    retry_count=retry_count,
                ) from exc
            except Exception as exc:
                raise EmbeddingUnavailableError(
                    reason="http_client_error",
                    retry_count=retry_count,
                ) from exc

            provider_request_id = _extract_provider_request_id(response)
            status_code = getattr(response, "status_code", None)
            if status_code == 200:
                return self._parse_success(
                    response,
                    input_count=len(texts),
                    retry_count=retry_count,
                    provider_request_id=provider_request_id,
                )
            retryable_status = status_code == 408 or status_code == 429 or (
                isinstance(status_code, int) and 500 <= status_code <= 599
            )
            if retryable_status and retry_count < self._runtime.max_retries:
                self._sleep_for_retry(retry_count, response)
                continue
            if status_code in {401, 403}:
                raise EmbeddingAuthError(
                    retry_count=retry_count,
                    provider_request_id=provider_request_id,
                )
            if status_code == 408:
                raise EmbeddingTimeoutError(
                    retry_count=retry_count,
                    provider_request_id=provider_request_id,
                )
            if status_code == 429:
                raise EmbeddingRateLimitError(
                    retry_count=retry_count,
                    provider_request_id=provider_request_id,
                )
            if isinstance(status_code, int) and 500 <= status_code <= 599:
                raise EmbeddingUnavailableError(
                    retry_count=retry_count,
                    provider_request_id=provider_request_id,
                )
            raise EmbeddingRequestRejectedError(
                retry_count=retry_count,
                provider_request_id=provider_request_id,
            )
        raise AssertionError("unreachable")

    def _load_httpx(self):
        try:
            import httpx
        except (ImportError, ModuleNotFoundError) as exc:
            raise EmbeddingDependencyError("embedding_api_extra_missing") from exc
        return httpx

    def _get_client(self, httpx: Any):
        if self._client is None:
            self._client = (
                self._client_factory()
                if self._client_factory is not None
                else httpx.Client()
            )
        return self._client

    def _parse_success(
        self,
        response: Any,
        *,
        input_count: int,
        retry_count: int,
        provider_request_id: str | None,
    ) -> EmbeddingBatchResult:
        try:
            payload = response.json()
        except Exception as exc:
            raise EmbeddingInvalidResponseError(
                "response_json_invalid",
                retry_count=retry_count,
                provider_request_id=provider_request_id,
            ) from exc
        if not isinstance(payload, dict):
            raise EmbeddingInvalidResponseError(
                "response_not_object",
                retry_count=retry_count,
                provider_request_id=provider_request_id,
            )
        response_model = payload.get("model")
        if response_model is not None and response_model != SILICONFLOW_MODEL_ID:
            raise EmbeddingInvalidResponseError(
                "response_model_mismatch",
                retry_count=retry_count,
                provider_request_id=provider_request_id,
            )
        data = payload.get("data")
        if not isinstance(data, list) or len(data) != input_count:
            raise EmbeddingInvalidResponseError(
                "response_data_count",
                retry_count=retry_count,
                provider_request_id=provider_request_id,
            )
        indexed: dict[int, object] = {}
        for item in data:
            if not isinstance(item, dict):
                raise EmbeddingInvalidResponseError(
                    "response_item_not_object",
                    retry_count=retry_count,
                    provider_request_id=provider_request_id,
                )
            index = item.get("index")
            if isinstance(index, bool) or not isinstance(index, int):
                raise EmbeddingInvalidResponseError(
                    "response_index_invalid",
                    retry_count=retry_count,
                    provider_request_id=provider_request_id,
                )
            if index < 0 or index >= input_count or index in indexed:
                raise EmbeddingInvalidResponseError(
                    "response_index_invalid",
                    retry_count=retry_count,
                    provider_request_id=provider_request_id,
                )
            if "embedding" not in item:
                raise EmbeddingInvalidResponseError(
                    "response_embedding_missing",
                    retry_count=retry_count,
                    provider_request_id=provider_request_id,
                )
            indexed[index] = item["embedding"]
        if set(indexed) != set(range(input_count)):
            raise EmbeddingInvalidResponseError(
                "response_index_incomplete",
                retry_count=retry_count,
                provider_request_id=provider_request_id,
            )
        try:
            raw = np.asarray([indexed[index] for index in range(input_count)], dtype=np.float64)
            vectors = normalize_and_validate_vectors(
                raw,
                input_count=input_count,
                dimension=self.dimension,
                reason_prefix="remote_output",
            )
        except EmbeddingInvalidResponseError as exc:
            raise EmbeddingInvalidResponseError(
                exc.reason,
                retry_count=retry_count,
                provider_request_id=provider_request_id,
            ) from exc
        except (TypeError, ValueError, OverflowError) as exc:
            raise EmbeddingInvalidResponseError(
                "remote_output_not_numeric",
                retry_count=retry_count,
                provider_request_id=provider_request_id,
            ) from exc
        try:
            input_tokens = _parse_input_tokens(payload.get("usage"))
        except EmbeddingInvalidResponseError as exc:
            raise EmbeddingInvalidResponseError(
                exc.reason,
                retry_count=retry_count,
                provider_request_id=provider_request_id,
            ) from exc
        return EmbeddingBatchResult(
            vectors=vectors,
            signature=self.signature,
            usage=EmbeddingUsage(input_count=input_count, input_tokens=input_tokens),
            diagnostics=EmbeddingCallDiagnostics(
                retry_count=retry_count,
                provider_request_id=provider_request_id,
            ),
        )

    def _sleep_for_retry(self, retry_count: int, response: Any | None) -> None:
        delay = self._runtime.retry_backoff_seconds * (2**retry_count)
        if response is not None:
            retry_after = _get_header(response, "retry-after")
            if retry_after is not None:
                try:
                    parsed = float(retry_after)
                except (TypeError, ValueError):
                    parsed = -1.0
                if 0.0 <= parsed <= 60.0:
                    delay = parsed
        if delay > 0.0:
            self._sleeper(delay)


def _get_header(response: Any, name: str) -> str | None:
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    try:
        value = headers.get(name)
    except (AttributeError, TypeError):
        return None
    return value if isinstance(value, str) else None


def _extract_provider_request_id(response: Any) -> str | None:
    for header in _REQUEST_ID_HEADERS:
        value = _get_header(response, header)
        if value is not None and _SAFE_REQUEST_ID_RE.fullmatch(value):
            return value
    return None


def _parse_input_tokens(usage: object) -> int | None:
    if usage is None:
        return None
    if not isinstance(usage, dict):
        raise EmbeddingInvalidResponseError("response_usage_invalid")
    value = usage.get("prompt_tokens")
    if value is None:
        value = usage.get("total_tokens")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise EmbeddingInvalidResponseError("response_usage_invalid")
    return value
