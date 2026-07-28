from __future__ import annotations

from app.errors import AemeathError, ErrorCode


class SemanticQueryConfigurationError(AemeathError):
    def __init__(self, *, reason: str):
        super().__init__(
            code=ErrorCode.CONFIG_INVALID,
            public_message="Semantic query configuration is invalid.",
            internal_message=f"Semantic query configuration is invalid ({reason}).",
            metadata={"reason": reason},
        )
        self.reason = reason


class SemanticCoordinatorUnhealthyError(AemeathError):
    def __init__(self, *, cause_error_code: ErrorCode):
        super().__init__(
            code=ErrorCode.SEMANTIC_COORDINATOR_UNHEALTHY,
            public_message="Semantic query runtime is unhealthy.",
            internal_message="Semantic query runtime is unhealthy.",
            retryable=False,
            metadata={
                "reason_code": "semantic_coordinator_unhealthy",
                "cause_error_code": cause_error_code.value,
            },
        )
        self.reason = "semantic_coordinator_unhealthy"
        self.cause_error_code = cause_error_code


class SemanticProviderSignatureMismatchError(AemeathError):
    def __init__(self):
        super().__init__(
            code=ErrorCode.EMBEDDING_SIGNATURE_MISMATCH,
            public_message="Embedding signature does not match the configured provider.",
            internal_message="Semantic query provider signature mismatch.",
            metadata={"reason": "semantic_provider_signature_mismatch"},
        )
        self.reason = "semantic_provider_signature_mismatch"
