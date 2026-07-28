from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from threading import RLock

import numpy as np

from app.embeddings.errors import EmbeddingError, EmbeddingInvalidResponseError
from app.embeddings.ports import EmbeddingProviderPort
from app.embeddings.schemas import EmbeddingRequest, ResolvedQueryEmbedding
from app.embeddings.validation import validate_normalized_vectors
from app.errors import AemeathError, ErrorCode, map_exception
from app.observability.noop import NoopAppLogger
from app.observability.ports import AppLoggerPort
from app.semantic.budget import SemanticQueryBudget, load_semantic_query_budget
from app.semantic.errors import (
    SemanticCoordinatorUnhealthyError,
    SemanticProviderSignatureMismatchError,
    SemanticQueryConfigurationError,
)
from app.semantic.observability import record_semantic_context, record_semantic_failure
from app.semantic.query_builder import BuiltSemanticQuery, NeutralTurnQueryBuilderV01
from app.semantic.schemas import (
    SEMANTIC_QUERY_ROLES,
    SemanticContextStatus,
    SemanticQueryRequest,
    SemanticQueryResolution,
    SemanticQueryRole,
    SemanticRoleStatus,
    SemanticTurnInput,
    TurnSemanticContext,
)

_TRANSIENT_CODES = {
    ErrorCode.EMBEDDING_TIMEOUT,
    ErrorCode.EMBEDDING_RATE_LIMITED,
    ErrorCode.EMBEDDING_UNAVAILABLE,
}
_PERMANENT_CODES = {
    ErrorCode.EMBEDDING_CONFIG_INVALID,
    ErrorCode.EMBEDDING_SECRET_MISSING,
    ErrorCode.EMBEDDING_DEPENDENCY_MISSING,
    ErrorCode.EMBEDDING_AUTH_FAILED,
    ErrorCode.EMBEDDING_QUALIFICATION_FAILED,
}
_CONTRACT_CODES = {
    ErrorCode.EMBEDDING_INVALID_RESPONSE,
    ErrorCode.EMBEDDING_SIGNATURE_MISMATCH,
    ErrorCode.EMBEDDING_VECTOR_CORRUPT,
}
_REQUEST_CODES = {
    ErrorCode.INVALID_INPUT,
    ErrorCode.EMBEDDING_INVALID_INPUT,
    ErrorCode.EMBEDDING_REQUEST_REJECTED,
}


@dataclass(frozen=True)
class _UnhealthyState:
    reason_code: str
    cause_error_code: ErrorCode


class SemanticQueryCoordinator:
    def __init__(
        self,
        *,
        provider: EmbeddingProviderPort,
        active_roles: Iterable[SemanticQueryRole],
        query_builder: NeutralTurnQueryBuilderV01 | None = None,
        query_budget: SemanticQueryBudget | None = None,
        app_logger: AppLoggerPort | None = None,
        clock: Callable[[], float] = time.perf_counter,
    ):
        configured_roles = tuple(active_roles)
        if len(set(configured_roles)) != len(configured_roles):
            raise SemanticQueryConfigurationError(reason="semantic_role_duplicate")
        if any(role not in SEMANTIC_QUERY_ROLES for role in configured_roles):
            raise SemanticQueryConfigurationError(reason="semantic_role_invalid")
        configured_set = set(configured_roles)
        self._active_roles = tuple(
            role for role in SEMANTIC_QUERY_ROLES if role in configured_set
        )
        self._provider = provider
        self._query_builder = query_builder or NeutralTurnQueryBuilderV01()
        self._query_budget = query_budget or load_semantic_query_budget()
        self._app_logger = app_logger or NoopAppLogger()
        self._clock = clock
        self._state_lock = RLock()
        self._unhealthy_state: _UnhealthyState | None = None

    def prepare_turn_context(
        self,
        *,
        turn_input: SemanticTurnInput,
    ) -> TurnSemanticContext:
        started_at = self._clock()
        if not self._active_roles:
            context = self._context_without_provider(
                turn_input=turn_input,
                built=None,
                status="not_requested",
                reason_code="semantic_role_not_requested",
                started_at=started_at,
            )
            self._record_context(context, turn_input)
            return context

        try:
            self._raise_if_unhealthy()
        except SemanticCoordinatorUnhealthyError as exc:
            record_semantic_failure(
                self._app_logger,
                turn_input=turn_input,
                duration_ms=self._duration_ms(started_at),
                reason_code="semantic_coordinator_unhealthy",
                error_code=exc.code.value,
                provider=self._provider.signature.provider,
                profile_id=self._provider.signature.profile_id,
            )
            raise

        built = self._query_builder.build(
            turn_input=turn_input,
            budget=self._query_budget,
        )
        if built.status == "skipped":
            context = self._context_without_provider(
                turn_input=turn_input,
                built=built,
                status="skipped",
                reason_code=built.reason_code,
                started_at=started_at,
            )
            self._record_context(context, turn_input)
            return context

        assert built.canonical_query is not None
        requests = tuple(
            SemanticQueryRequest(
                role=role,
                construction_version=built.construction_version,
                budget_version=built.budget_version,
                query_strategy=built.query_strategy,
                canonical_query=built.canonical_query,
                dependency_message_ids=built.dependency_message_ids,
            )
            for role in self._active_roles
        )
        try:
            resolved_by_role, unique_query_count = self._resolve_requests(requests)
        except Exception as exc:
            duration_ms = self._duration_ms(started_at)
            error_code = self._error_code(exc)
            if error_code in _TRANSIENT_CODES:
                context = self._failed_context(
                    turn_input=turn_input,
                    built=built,
                    error_code=error_code,
                    duration_ms=duration_ms,
                )
                self._record_context(context, turn_input)
                return context
            record_semantic_failure(
                self._app_logger,
                turn_input=turn_input,
                duration_ms=duration_ms,
                reason_code=self._failure_reason(exc, error_code),
                error_code=error_code.value,
                provider=self._provider.signature.provider,
                profile_id=self._provider.signature.profile_id,
            )
            raise

        context = self._ready_context(
            turn_input=turn_input,
            built=built,
            resolved_by_role=resolved_by_role,
            unique_query_count=unique_query_count,
            duration_ms=self._duration_ms(started_at),
        )
        self._record_context(context, turn_input)
        return context

    def _resolve_requests(
        self,
        requests: tuple[SemanticQueryRequest, ...],
    ) -> tuple[dict[SemanticQueryRole, ResolvedQueryEmbedding], int]:
        with self._state_lock:
            if self._unhealthy_state is not None:
                raise SemanticCoordinatorUnhealthyError(
                    cause_error_code=self._unhealthy_state.cause_error_code
                )
            try:
                provider_signature = self._provider.signature.canonical()
                unique_queries: list[str] = []
                unique_indexes: dict[tuple[str, str, str], int] = {}
                role_indexes: dict[SemanticQueryRole, int] = {}
                for request in requests:
                    dedup_key = (
                        provider_signature,
                        "query",
                        request.canonical_query,
                    )
                    batch_index = unique_indexes.get(dedup_key)
                    if batch_index is None:
                        batch_index = len(unique_queries)
                        unique_indexes[dedup_key] = batch_index
                        unique_queries.append(request.canonical_query)
                    role_indexes[request.role] = batch_index
                result = self._provider.embed(
                    EmbeddingRequest(texts=tuple(unique_queries), purpose="query")
                )
                try:
                    result_signature = result.signature.canonical()
                except Exception as exc:
                    raise EmbeddingInvalidResponseError(
                        "semantic_result_signature_invalid"
                    ) from exc
                if result_signature != provider_signature:
                    raise SemanticProviderSignatureMismatchError()
                try:
                    result_vectors = result.vectors
                    result_count = result_vectors.shape[0]
                except (AttributeError, IndexError, TypeError) as exc:
                    raise EmbeddingInvalidResponseError(
                        "semantic_result_vectors_invalid"
                    ) from exc
                if result_count != len(unique_queries):
                    raise EmbeddingInvalidResponseError(
                        "semantic_result_count_mismatch"
                    )
                validated = validate_normalized_vectors(
                    result_vectors,
                    input_count=len(unique_queries),
                    dimension=result.signature.dimension,
                )
            except Exception as exc:
                error_code = self._error_code(exc)
                if error_code in _PERMANENT_CODES | _CONTRACT_CODES:
                    self._unhealthy_state = _UnhealthyState(
                        reason_code="semantic_coordinator_unhealthy",
                        cause_error_code=error_code,
                    )
                raise

            resolved_by_index: dict[int, ResolvedQueryEmbedding] = {}
            for batch_index in range(len(unique_queries)):
                vector = np.ascontiguousarray(
                    validated[batch_index], dtype="<f4"
                ).copy()
                vector.setflags(write=False)
                resolved_by_index[batch_index] = ResolvedQueryEmbedding(
                    signature=result.signature,
                    vector=vector,
                )
            return (
                {
                    role: resolved_by_index[batch_index]
                    for role, batch_index in role_indexes.items()
                },
                len(unique_queries),
            )

    def _raise_if_unhealthy(self) -> None:
        with self._state_lock:
            if self._unhealthy_state is not None:
                raise SemanticCoordinatorUnhealthyError(
                    cause_error_code=self._unhealthy_state.cause_error_code
                )

    def _ready_context(
        self,
        *,
        turn_input: SemanticTurnInput,
        built: BuiltSemanticQuery,
        resolved_by_role: Mapping[SemanticQueryRole, ResolvedQueryEmbedding],
        unique_query_count: int,
        duration_ms: float,
    ) -> TurnSemanticContext:
        return self._make_context(
            turn_input=turn_input,
            built=built,
            status="ready",
            duration_ms=duration_ms,
            unique_query_count=unique_query_count,
            provider_called=True,
            active_status="ready",
            active_reason_code="query_ready",
            resolved_by_role=resolved_by_role,
            error_code=None,
        )

    def _failed_context(
        self,
        *,
        turn_input: SemanticTurnInput,
        built: BuiltSemanticQuery,
        error_code: ErrorCode,
        duration_ms: float,
    ) -> TurnSemanticContext:
        return self._make_context(
            turn_input=turn_input,
            built=built,
            status="failed",
            duration_ms=duration_ms,
            unique_query_count=1,
            provider_called=True,
            active_status="failed",
            active_reason_code="embedding_transient_failure",
            resolved_by_role=None,
            error_code=error_code.value,
        )

    def _context_without_provider(
        self,
        *,
        turn_input: SemanticTurnInput,
        built: BuiltSemanticQuery | None,
        status: SemanticContextStatus,
        reason_code: str,
        started_at: float,
    ) -> TurnSemanticContext:
        return self._make_context(
            turn_input=turn_input,
            built=built,
            status=status,
            duration_ms=self._duration_ms(started_at),
            unique_query_count=0,
            provider_called=False,
            active_status=status,
            active_reason_code=reason_code,
            resolved_by_role=None,
            error_code=None,
        )

    def _make_context(
        self,
        *,
        turn_input: SemanticTurnInput,
        built: BuiltSemanticQuery | None,
        status: SemanticContextStatus,
        duration_ms: float,
        unique_query_count: int,
        provider_called: bool,
        active_status: SemanticRoleStatus,
        active_reason_code: str,
        resolved_by_role: Mapping[SemanticQueryRole, ResolvedQueryEmbedding] | None,
        error_code: str | None,
    ) -> TurnSemanticContext:
        active_set = set(self._active_roles)
        resolutions = tuple(
            SemanticQueryResolution(
                role=role,
                status=active_status if role in active_set else "not_requested",
                reason_code=(
                    active_reason_code
                    if role in active_set
                    else "semantic_role_not_requested"
                ),
                request_key="q0" if role in active_set and unique_query_count else None,
                resolved_embedding=(
                    resolved_by_role.get(role)
                    if role in active_set and resolved_by_role is not None
                    else None
                ),
                error_code=error_code if role in active_set else None,
            )
            for role in SEMANTIC_QUERY_ROLES
        )
        signature = self._provider.signature
        return TurnSemanticContext(
            turn_id=turn_input.turn_id,
            status=status,
            construction_version=(
                built.construction_version
                if built is not None
                else self._query_builder.construction_version
            ),
            budget_version=(
                built.budget_version if built is not None else self._query_budget.version
            ),
            query_strategy=built.query_strategy if built is not None else "none",
            budget_reason_codes=(
                built.budget_reason_codes if built is not None else ()
            ),
            provider=signature.provider,
            profile_id=signature.profile_id,
            signature_fingerprint=signature.fingerprint(),
            requested_role_count=len(self._active_roles),
            unique_query_count=unique_query_count,
            dependency_message_count=(
                len(built.dependency_message_ids) if built is not None else 0
            ),
            query_char_count=built.query_char_count if built is not None else 0,
            query_utf8_bytes=built.query_utf8_bytes if built is not None else 0,
            provider_called=provider_called,
            duration_ms=duration_ms,
            resolutions=resolutions,
        )

    def _record_context(
        self,
        context: TurnSemanticContext,
        turn_input: SemanticTurnInput,
    ) -> None:
        record_semantic_context(
            self._app_logger,
            context=context,
            user_id=turn_input.user_id,
            session_id=turn_input.session_id,
            character_id=turn_input.character_id,
        )

    def _duration_ms(self, started_at: float) -> float:
        return max(0.0, (self._clock() - started_at) * 1000.0)

    @staticmethod
    def _error_code(exc: BaseException) -> ErrorCode:
        if isinstance(exc, AemeathError):
            return exc.code
        return map_exception(exc).code

    @staticmethod
    def _failure_reason(exc: BaseException, error_code: ErrorCode) -> str:
        if isinstance(exc, SemanticCoordinatorUnhealthyError):
            return "semantic_coordinator_unhealthy"
        if error_code in _PERMANENT_CODES:
            return "embedding_permanent_failure"
        if error_code in _CONTRACT_CODES:
            if error_code == ErrorCode.EMBEDDING_SIGNATURE_MISMATCH:
                return "provider_signature_mismatch"
            if (
                isinstance(exc, EmbeddingInvalidResponseError)
                and exc.reason == "semantic_result_count_mismatch"
            ):
                return "result_count_mismatch"
            return "query_vector_invalid"
        if error_code in _REQUEST_CODES or isinstance(exc, EmbeddingError):
            return "embedding_request_failed"
        return "embedding_request_failed"
