from __future__ import annotations

from typing import Any

from app.errors.base import AemeathError
from app.errors.codes import ErrorCode


class EmbeddingError(AemeathError):
    def __init__(
        self,
        *,
        code: ErrorCode,
        public_message: str,
        reason: str,
        retryable: bool = False,
        retry_count: int = 0,
        provider_request_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ):
        safe_metadata: dict[str, Any] = dict(metadata or {})
        safe_metadata["reason"] = reason
        safe_metadata["retry_count"] = retry_count
        if provider_request_id is not None:
            safe_metadata["provider_request_id"] = provider_request_id
        super().__init__(
            code=code,
            public_message=public_message,
            internal_message=f"Embedding operation failed ({reason}).",
            retryable=retryable,
            metadata=safe_metadata,
        )
        self.reason = reason
        self.retry_count = retry_count
        self.provider_request_id = provider_request_id

    def add_retry_offset(self, retry_offset: int) -> None:
        if retry_offset <= 0:
            return
        self.retry_count += retry_offset
        self.metadata["retry_count"] = self.retry_count


class EmbeddingInvalidInputError(EmbeddingError):
    def __init__(self, reason: str):
        super().__init__(
            code=ErrorCode.EMBEDDING_INVALID_INPUT,
            public_message="Embedding input is invalid.",
            reason=reason,
        )


class EmbeddingConfigurationError(EmbeddingError):
    def __init__(self, reason: str):
        super().__init__(
            code=ErrorCode.EMBEDDING_CONFIG_INVALID,
            public_message="Embedding configuration is invalid.",
            reason=reason,
        )


class EmbeddingDependencyError(EmbeddingError):
    def __init__(self, reason: str):
        super().__init__(
            code=ErrorCode.EMBEDDING_DEPENDENCY_MISSING,
            public_message="An optional embedding dependency is not installed.",
            reason=reason,
        )


class EmbeddingSecretMissingError(EmbeddingError):
    def __init__(self):
        super().__init__(
            code=ErrorCode.EMBEDDING_SECRET_MISSING,
            public_message="Embedding service credentials are missing.",
            reason="siliconflow_api_key_missing",
        )


class EmbeddingRequestRejectedError(EmbeddingError):
    def __init__(
        self,
        *,
        retry_count: int = 0,
        provider_request_id: str | None = None,
    ):
        super().__init__(
            code=ErrorCode.EMBEDDING_REQUEST_REJECTED,
            public_message="The embedding service rejected the request.",
            reason="request_rejected",
            retry_count=retry_count,
            provider_request_id=provider_request_id,
        )


class EmbeddingAuthError(EmbeddingError):
    def __init__(self, *, retry_count: int = 0, provider_request_id: str | None = None):
        super().__init__(
            code=ErrorCode.EMBEDDING_AUTH_FAILED,
            public_message="The embedding service rejected authentication.",
            reason="authentication_rejected",
            retry_count=retry_count,
            provider_request_id=provider_request_id,
        )


class EmbeddingRateLimitError(EmbeddingError):
    def __init__(self, *, retry_count: int, provider_request_id: str | None = None):
        super().__init__(
            code=ErrorCode.EMBEDDING_RATE_LIMITED,
            public_message="The embedding service is rate limited.",
            reason="rate_limited",
            retryable=True,
            retry_count=retry_count,
            provider_request_id=provider_request_id,
        )


class EmbeddingTimeoutError(EmbeddingError):
    def __init__(self, *, retry_count: int, provider_request_id: str | None = None):
        super().__init__(
            code=ErrorCode.EMBEDDING_TIMEOUT,
            public_message="The embedding request timed out.",
            reason="request_timeout",
            retryable=True,
            retry_count=retry_count,
            provider_request_id=provider_request_id,
        )


class EmbeddingUnavailableError(EmbeddingError):
    def __init__(
        self,
        *,
        reason: str = "service_unavailable",
        retry_count: int = 0,
        provider_request_id: str | None = None,
    ):
        super().__init__(
            code=ErrorCode.EMBEDDING_UNAVAILABLE,
            public_message="The embedding service is temporarily unavailable.",
            reason=reason,
            retryable=True,
            retry_count=retry_count,
            provider_request_id=provider_request_id,
        )


class EmbeddingInvalidResponseError(EmbeddingError):
    def __init__(
        self,
        reason: str,
        *,
        retry_count: int = 0,
        provider_request_id: str | None = None,
    ):
        super().__init__(
            code=ErrorCode.EMBEDDING_INVALID_RESPONSE,
            public_message="The embedding service returned an invalid response.",
            reason=reason,
            retry_count=retry_count,
            provider_request_id=provider_request_id,
        )


class EmbeddingVectorCorruptError(EmbeddingError):
    def __init__(self, reason: str):
        super().__init__(
            code=ErrorCode.EMBEDDING_VECTOR_CORRUPT,
            public_message="Embedding vector data is invalid.",
            reason=reason,
        )


class EmbeddingQualificationError(EmbeddingError):
    def __init__(self, reason: str):
        super().__init__(
            code=ErrorCode.EMBEDDING_QUALIFICATION_FAILED,
            public_message="Embedding profile qualification failed.",
            reason=reason,
        )
