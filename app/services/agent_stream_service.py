from __future__ import annotations

import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Condition, Lock, Thread, current_thread
from time import monotonic
from typing import Any, Iterator, Literal

from app.agent.turn_engine import DialogueTurnEngine, PreparedDialogueTurn, TurnPreflight
from app.db.stream_turns import (
    StreamRequestRecord,
    StreamTurnComplete,
    StreamTurnFail,
    StreamTurnPersistencePort,
    StreamTurnResolveOrReserve,
    StreamTurnResolveOutcome,
)
from app.errors import AemeathError, ErrorCode, map_exception, redact_sensitive
from app.model.streaming import (
    ModelStreamFinished,
    ModelStreamRequest,
    ModelTextDelta,
    StreamingChatModelClient,
)
from app.observability.noop import NoopAppLogger
from app.observability.ports import AppLoggerPort
from app.observability.schemas import AppLogRecord
from app.quota.errors import UsageQuotaExceededError
from app.quota.ports import StreamQuotaTurnPersistencePort, UsageQuotaPolicyPort
from app.quota.schemas import (
    StreamQuotaComplete,
    StreamQuotaFail,
    StreamQuotaProviderStarted,
    StreamQuotaReservationRecord,
    StreamQuotaResolveOrReserve,
    StreamQuotaResolveOutcome,
    StreamQuotaTerminalRecord,
    UsageOutputEstimateRequest,
    UsagePromptEstimateRequest,
    UsageQuotaPlan,
    UsageQuotaPlanRequest,
)
from app.quota.service import prompt_messages_utf8_bytes
from app.runtime.guardrails.errors import RuntimeBusyError
from app.runtime.guardrails.ports import (
    ChatInputLimitPolicyPort,
    DialogueAdmissionHandle,
    DialogueAdmissionPort,
)
from app.runtime.guardrails.schemas import ChatInputLimitRequest, DialogueAdmissionRequest
from app.runtime.streaming import ActiveStreamRegistry
from app.schemas import AgentTurnInput
from app.services.schemas import UserContext
from app.services.stream_fingerprint import FINGERPRINT_VERSION_V1, fingerprint_chat_request_v1
from app.services.stream_schemas import (
    ChatStreamEvent,
    PublicStreamError,
    ResponseDelta,
    StreamChatRequest,
    StreamConsumerSlowError,
    StreamEventSink,
    TurnCompleted,
    TurnFailed,
    TurnStarted,
)
from app.utils.ids import new_id

_CLIENT_MESSAGE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_CANCELLATION_REASONS = frozenset(
    {
        "user_cancelled",
        "client_disconnected",
        "sse_generator_closed",
        "consumer_slow",
        "runtime_closing",
    }
)
_MODEL_ERROR_CODES = frozenset(
    {
        ErrorCode.MODEL_BAD_REQUEST,
        ErrorCode.MODEL_AUTH_FAILED,
        ErrorCode.MODEL_RATE_LIMITED,
        ErrorCode.MODEL_TIMEOUT,
        ErrorCode.MODEL_UNAVAILABLE,
        ErrorCode.MODEL_INVALID_RESPONSE,
    }
)


@dataclass(frozen=True)
class StreamServiceLimits:
    response_max_chars: int = 32_768
    response_max_utf8_bytes: int = 98_304
    public_delta_max_utf8_bytes: int = 4_096
    producer_put_timeout_seconds: float = 5.0
    producer_join_timeout_seconds: float = 16.0

    def __post_init__(self) -> None:
        integer_limits = (
            self.response_max_chars,
            self.response_max_utf8_bytes,
            self.public_delta_max_utf8_bytes,
        )
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value <= 0
            for value in integer_limits
        ):
            raise ValueError("stream_service_integer_limit_invalid")
        if self.public_delta_max_utf8_bytes < 4:
            raise ValueError("stream_public_delta_limit_too_small")
        if self.producer_put_timeout_seconds <= 0 or self.producer_join_timeout_seconds <= 0:
            raise ValueError("stream_service_timeout_invalid")


@dataclass(frozen=True)
class StreamQuotaSettings:
    daily_turn_limit: int
    daily_token_limit: int
    retry_after_seconds: int
    estimator_version: str
    context_budget_max_chars: int
    max_output_tokens: int

    def __post_init__(self) -> None:
        for value in (
            self.daily_turn_limit,
            self.daily_token_limit,
            self.context_budget_max_chars,
            self.max_output_tokens,
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError("stream_quota_positive_integer_required")
        if (
            not isinstance(self.retry_after_seconds, int)
            or isinstance(self.retry_after_seconds, bool)
            or self.retry_after_seconds < 0
        ):
            raise ValueError("stream_quota_retry_after_invalid")
        if not isinstance(self.estimator_version, str) or not self.estimator_version:
            raise ValueError("stream_quota_estimator_version_required")


class CancellationToken:
    def __init__(self) -> None:
        self._lock = Lock()
        self._reason: str | None = None

    def request_cancel(self, *, reason: str) -> bool:
        if reason not in _CANCELLATION_REASONS:
            raise ValueError("stream_cancellation_reason_invalid")
        with self._lock:
            if self._reason is not None:
                return False
            self._reason = reason
            return True

    def is_cancelled(self) -> bool:
        with self._lock:
            return self._reason is not None

    def reason(self) -> str | None:
        with self._lock:
            return self._reason


@dataclass
class _KeyEntry:
    lock: Lock
    reference_count: int = 0


class RefCountedStreamKeyRegistry:
    """Bound duplicate-key coordination without retaining client identifiers."""

    def __init__(self, *, hard_cap: int = 128, wait_timeout_seconds: float = 5.0) -> None:
        if hard_cap != 128:
            raise ValueError("stream_key_registry_hard_cap_must_be_128")
        if wait_timeout_seconds < 0:
            raise ValueError("stream_key_registry_wait_timeout_invalid")
        self._hard_cap = hard_cap
        self._wait_timeout_seconds = wait_timeout_seconds
        self._lock = Lock()
        self._entries: dict[tuple[str, str, str], _KeyEntry] = {}

    @contextmanager
    def acquire(self, *, key: tuple[str, str, str]) -> Iterator[None]:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                if len(self._entries) >= self._hard_cap:
                    raise RuntimeBusyError("stream_key_registry_capacity")
                entry = _KeyEntry(lock=Lock())
                self._entries[key] = entry
            entry.reference_count += 1
        acquired = entry.lock.acquire(timeout=self._wait_timeout_seconds)
        if not acquired:
            self._dereference(key=key, entry=entry)
            raise RuntimeBusyError("stream_key_wait_timeout")
        try:
            yield
        finally:
            entry.lock.release()
            self._dereference(key=key, entry=entry)

    def _dereference(self, *, key: tuple[str, str, str], entry: _KeyEntry) -> None:
        with self._lock:
            entry.reference_count -= 1
            if entry.reference_count == 0 and self._entries.get(key) is entry:
                del self._entries[key]

    @property
    def entry_count(self) -> int:
        with self._lock:
            return len(self._entries)


class AgentStreamService:
    def __init__(
        self,
        *,
        turn_engine: DialogueTurnEngine,
        model_client: StreamingChatModelClient,
        persistence: StreamTurnPersistencePort,
        admission: DialogueAdmissionPort,
        input_limit_policy: ChatInputLimitPolicyPort,
        active_stream_registry: ActiveStreamRegistry,
        owner_runtime_id: str,
        quota_persistence: StreamQuotaTurnPersistencePort | None = None,
        quota_policy: UsageQuotaPolicyPort | None = None,
        quota_settings: StreamQuotaSettings | None = None,
        limits: StreamServiceLimits | None = None,
        key_registry: RefCountedStreamKeyRegistry | None = None,
        app_logger: AppLoggerPort | None = None,
    ) -> None:
        if not owner_runtime_id:
            raise ValueError("stream_owner_runtime_id_required")
        quota_parts = (quota_persistence, quota_policy, quota_settings)
        if any(part is None for part in quota_parts) and not all(
            part is None for part in quota_parts
        ):
            raise ValueError("stream_quota_dependencies_incomplete")
        self.turn_engine = turn_engine
        self.model_client = model_client
        self.persistence = persistence
        self.quota_persistence = quota_persistence
        self.quota_policy = quota_policy
        self.quota_settings = quota_settings
        self.admission = admission
        self.input_limit_policy = input_limit_policy
        self.active_stream_registry = active_stream_registry
        self.owner_runtime_id = owner_runtime_id
        self.limits = limits or StreamServiceLimits()
        self.key_registry = key_registry or RefCountedStreamKeyRegistry()
        self.app_logger = app_logger or NoopAppLogger()

    @property
    def quota_enabled(self) -> bool:
        return (
            self.quota_persistence is not None
            and self.quota_policy is not None
            and self.quota_settings is not None
        )

    def stream_chat(
        self,
        *,
        user_context: UserContext,
        request: StreamChatRequest,
    ) -> _ChatStreamHandle:
        started_at = monotonic()
        if not self.active_stream_registry.is_open:
            raise RuntimeBusyError("stream_registry_closing")
        client_metadata = self._validate_and_copy_request(request=request)
        fingerprint = fingerprint_chat_request_v1(
            character_id=request.character_id,
            message=request.message,
            metadata=client_metadata,
        )
        turn_metadata = dict(client_metadata)
        request_id = user_context.request_id or new_id("req")
        turn_metadata["request_id"] = request_id
        self.input_limit_policy.validate(
            request=ChatInputLimitRequest(
                message=request.message,
                metadata=turn_metadata,
            )
        )
        key = (user_context.user_id, request.session_id, request.client_message_id)
        with self.key_registry.acquire(key=key):
            admission_handle = self.admission.open_handle(
                request=DialogueAdmissionRequest(
                    user_id=user_context.user_id,
                    session_id=request.session_id,
                    request_id=request_id,
                )
            )
            handle: _ChatStreamHandle | None = None
            try:
                candidate_turn_id = new_id("turn")
                turn_input = AgentTurnInput(
                    user_id=user_context.user_id,
                    session_id=request.session_id,
                    character_id=request.character_id,
                    message=request.message,
                    metadata=turn_metadata,
                )
                character_resolution = self.turn_engine.resolve_character(
                    requested_character_id=request.character_id
                )
                reserve_request = StreamTurnResolveOrReserve(
                    user_id=user_context.user_id,
                    session_id=request.session_id,
                    active_character_id=character_resolution.active_character_id,
                    client_message_id=request.client_message_id,
                    request_fingerprint_version=FINGERPRINT_VERSION_V1,
                    request_fingerprint=fingerprint,
                    owner_runtime_id=self.owner_runtime_id,
                    turn_id=candidate_turn_id,
                    user_message_id=new_id("message"),
                    user_message_content=request.message,
                )
                outcome = self._resolve_or_reserve(
                    reserve_request=reserve_request,
                    active_character_id=character_resolution.active_character_id,
                    user_context=user_context,
                    request=request,
                    request_id=request_id,
                )
                outcome = self._resolve_local_in_progress(
                    outcome=outcome,
                    user_context=user_context,
                    request=request,
                )
                if outcome.kind == "conflict":
                    raise _chat_request_error(conflict=True)
                if outcome.kind == "in_progress":
                    raise _chat_request_error(conflict=False)

                owns_in_progress = outcome.kind == "reserved"
                preflight = None
                if owns_in_progress:
                    try:
                        preflight = self.turn_engine.preflight(
                            turn_input=turn_input,
                            turn_id=candidate_turn_id,
                            record_started=False,
                            streaming_safe_observability=True,
                            character_resolution=character_resolution,
                        )
                    except Exception as exc:
                        error = map_exception(exc)
                        self._fail_reserved_turn(
                            record=outcome.record,
                            terminal_error_code=error.code.value,
                            terminal_retryable=error.retryable,
                            terminal_reason="preparation_failed",
                            request_id=request_id,
                            request=request,
                        )
                        raise
                handle = _ChatStreamHandle(
                    request_id=request_id,
                    outcome=outcome,
                    preflight=preflight,
                    turn_engine=self.turn_engine,
                    model_client=self.model_client,
                    persistence=self.persistence,
                    quota_persistence=self.quota_persistence,
                    quota_policy=self.quota_policy,
                    quota_settings=self.quota_settings,
                    admission_handle=admission_handle,
                    active_stream_registry=self.active_stream_registry,
                    owner_runtime_id=self.owner_runtime_id,
                    limits=self.limits,
                    app_logger=self.app_logger,
                    started_at=started_at,
                )
                if owns_in_progress:
                    assert preflight is not None
                    self.turn_engine.record_started(preflight=preflight)
                self.active_stream_registry.register(handle)
                self._record(
                    event_name=(
                        "agent_stream.request_reserved"
                        if owns_in_progress
                        else "agent_stream.replayed"
                    ),
                    level="info",
                    request_id=request_id,
                    turn_id=handle.turn_id,
                    user_id=user_context.user_id,
                    request=request,
                    payload={"idempotency_outcome": outcome.kind},
                )
                return handle
            except BaseException as exc:
                if handle is None:
                    admission_handle.close()
                else:
                    reason = (
                        "runtime_closing"
                        if isinstance(exc, RuntimeBusyError)
                        and exc.reason == "stream_registry_closing"
                        else "sse_generator_closed"
                    )
                    try:
                        handle.cancel(reason=reason)
                        handle.close()
                    except BaseException:
                        try:
                            admission_handle.close()
                        except BaseException:
                            pass
                raise

    def _resolve_or_reserve(
        self,
        *,
        reserve_request: StreamTurnResolveOrReserve,
        active_character_id: str,
        user_context: UserContext,
        request: StreamChatRequest,
        request_id: str,
    ) -> StreamTurnResolveOutcome:
        if not self.quota_enabled:
            return self.persistence.resolve_or_reserve(request=reserve_request)
        assert self.quota_persistence is not None
        assert self.quota_policy is not None
        assert self.quota_settings is not None
        quota_plan = self.quota_policy.plan_stream_reservation(
            request=UsageQuotaPlanRequest(
                user_id=user_context.user_id,
                quota_day=_utc_quota_day(),
                surface="stream_chat",
                character_id=active_character_id,
                turn_limit=self.quota_settings.daily_turn_limit,
                token_limit=self.quota_settings.daily_token_limit,
                estimator_version=self.quota_settings.estimator_version,
                context_budget_max_chars=self.quota_settings.context_budget_max_chars,
                max_output_tokens=self.quota_settings.max_output_tokens,
            )
        )
        quota_outcome = self.quota_persistence.resolve_or_reserve_with_quota(
            request=StreamQuotaResolveOrReserve(
                stream_request=reserve_request,
                quota_plan=quota_plan,
                retry_after_seconds=self.quota_settings.retry_after_seconds,
            )
        )
        if quota_outcome.kind == "quota_exceeded":
            self._record_quota_rejected(
                request_id=request_id,
                user_id=user_context.user_id,
                request=request,
                quota_plan=quota_plan,
                retry_after_seconds=(
                    quota_outcome.retry_after_seconds
                    if quota_outcome.retry_after_seconds is not None
                    else self.quota_settings.retry_after_seconds
                ),
            )
            raise UsageQuotaExceededError(
                retry_after_seconds=(
                    quota_outcome.retry_after_seconds
                    if quota_outcome.retry_after_seconds is not None
                    else self.quota_settings.retry_after_seconds
                )
            )
        if quota_outcome.kind == "reserved" and quota_outcome.quota_reservation is not None:
            self._record_quota_reserved(
                request_id=request_id,
                user_id=user_context.user_id,
                request=request,
                reservation=quota_outcome.quota_reservation,
            )
        return _stream_outcome_from_quota(quota_outcome)

    def _fail_reserved_turn(
        self,
        *,
        record: StreamRequestRecord,
        terminal_error_code: str,
        terminal_retryable: bool,
        terminal_reason: str,
        request_id: str | None = None,
        request: StreamChatRequest | None = None,
    ) -> StreamRequestRecord:
        if not self.quota_enabled:
            return self.persistence.fail(
                request=StreamTurnFail(
                    record_id=record.id,
                    turn_id=record.turn_id,
                    user_id=record.user_id,
                    session_id=record.session_id,
                    owner_runtime_id=self.owner_runtime_id,
                    terminal_error_code=terminal_error_code,
                    terminal_retryable=terminal_retryable,
                    terminal_reason=terminal_reason,
                )
            )
        assert self.quota_persistence is not None
        terminal = self.quota_persistence.fail_with_quota(
            request=StreamQuotaFail(
                stream_request_id=record.id,
                turn_id=record.turn_id,
                user_id=record.user_id,
                session_id=record.session_id,
                owner_runtime_id=self.owner_runtime_id,
                terminal_error_code=terminal_error_code,
                terminal_retryable=terminal_retryable,
                terminal_reason=terminal_reason,
            )
        )
        if terminal.quota_reservation is not None:
            log_request = request or StreamChatRequest(
                session_id=record.session_id,
                character_id=record.active_character_id,
                message="",
                client_message_id=record.client_message_id,
                metadata={},
            )
            self._record(
                event_name=(
                    "usage_quota.settled"
                    if terminal.quota_reservation.status == "settled"
                    else "usage_quota.released"
                ),
                level="info",
                request_id=request_id or "",
                turn_id=terminal.quota_reservation.turn_id,
                user_id=record.user_id,
                request=log_request,
                error_code=terminal.quota_reservation.terminal_error_code,
                payload=_quota_log_payload(terminal.quota_reservation),
            )
        return terminal.stream_record

    def _resolve_local_in_progress(
        self,
        *,
        outcome: StreamTurnResolveOutcome,
        user_context: UserContext,
        request: StreamChatRequest,
    ) -> StreamTurnResolveOutcome:
        if outcome.kind != "in_progress":
            return outcome
        if self.active_stream_registry.is_live_turn(turn_id=outcome.record.turn_id):
            return outcome
        record = self._fail_reserved_turn(
            record=outcome.record,
            terminal_error_code=ErrorCode.CHAT_STREAM_INTERRUPTED.value,
            terminal_retryable=False,
            terminal_reason="runtime_orphaned",
            request_id=user_context.request_id,
            request=request,
        )
        return StreamTurnResolveOutcome(kind="replay_failed", record=record)

    def _record_quota_rejected(
        self,
        *,
        request_id: str,
        user_id: str,
        request: StreamChatRequest,
        quota_plan: UsageQuotaPlan,
        retry_after_seconds: int,
    ) -> None:
        self._record(
            event_name="usage_quota.rejected",
            level="warning",
            request_id=request_id,
            turn_id=None,
            user_id=user_id,
            request=request,
            error_code=ErrorCode.USAGE_QUOTA_EXCEEDED.value,
            payload={
                "quota_day": quota_plan.quota_day,
                "surface": quota_plan.surface,
                "estimator_version": quota_plan.estimator_version,
                "reserved_turns": quota_plan.reserved_turns,
                "reserved_tokens": quota_plan.reserved_tokens,
                "turn_limit": quota_plan.turn_limit,
                "token_limit": quota_plan.token_limit,
                "retry_after_seconds": retry_after_seconds,
            },
        )

    def _record_quota_reserved(
        self,
        *,
        request_id: str,
        user_id: str,
        request: StreamChatRequest,
        reservation: StreamQuotaReservationRecord,
    ) -> None:
        self._record(
            event_name="usage_quota.reserved",
            level="info",
            request_id=request_id,
            turn_id=reservation.turn_id,
            user_id=user_id,
            request=request,
            payload=_quota_log_payload(reservation),
        )

    def _validate_and_copy_request(self, *, request: StreamChatRequest) -> dict[str, Any]:
        if not isinstance(request.session_id, str) or not request.session_id:
            raise _invalid_input("stream_session_id_invalid")
        if not isinstance(request.character_id, str) or not request.character_id:
            raise _invalid_input("stream_character_id_invalid")
        if not isinstance(request.message, str):
            raise _invalid_input("stream_message_invalid")
        if not isinstance(request.client_message_id, str) or not _CLIENT_MESSAGE_ID_RE.fullmatch(
            request.client_message_id
        ):
            raise _invalid_input("stream_client_message_id_invalid")
        if not isinstance(request.metadata, dict):
            raise _invalid_input("stream_metadata_invalid")
        try:
            fingerprint_chat_request_v1(
                character_id=request.character_id,
                message=request.message,
                metadata=request.metadata,
            )
        except (TypeError, ValueError, UnicodeError) as exc:
            raise _invalid_input("stream_metadata_invalid") from exc
        return _copy_json_value(request.metadata)

    def _record(
        self,
        *,
        event_name: str,
        level: str,
        request_id: str,
        turn_id: str | None,
        user_id: str,
        request: StreamChatRequest,
        payload: dict[str, Any],
        error_code: str | None = None,
    ) -> None:
        try:
            self.app_logger.record(
                AppLogRecord(
                    event_name=event_name,
                    level=level,
                    request_id=request_id,
                    turn_id=turn_id,
                    session_id=request.session_id,
                    user_id=user_id,
                    character_id=request.character_id,
                    error_code=error_code,
                    payload=redact_sensitive(payload),
                )
            )
        except Exception:
            return


class _ChatStreamHandle:
    def __init__(
        self,
        *,
        request_id: str,
        outcome: StreamTurnResolveOutcome,
        preflight: TurnPreflight | None,
        turn_engine: DialogueTurnEngine,
        model_client: StreamingChatModelClient,
        persistence: StreamTurnPersistencePort,
        quota_persistence: StreamQuotaTurnPersistencePort | None,
        quota_policy: UsageQuotaPolicyPort | None,
        quota_settings: StreamQuotaSettings | None,
        admission_handle: DialogueAdmissionHandle,
        active_stream_registry: ActiveStreamRegistry,
        owner_runtime_id: str,
        limits: StreamServiceLimits,
        app_logger: AppLoggerPort,
        started_at: float,
    ) -> None:
        self.handle_id = new_id("stream_handle")
        self.request_id = request_id
        self.turn_id = outcome.record.turn_id
        self.session_id = outcome.record.session_id
        self.started_event = TurnStarted(
            event_type="turn.started",
            request_id=request_id,
            turn_id=self.turn_id,
            session_id=self.session_id,
        )
        self.owns_in_progress_turn = outcome.kind == "reserved"
        self._outcome = outcome
        self._preflight = preflight
        self._turn_engine = turn_engine
        self._model_client = model_client
        self._persistence = persistence
        self._quota_persistence = quota_persistence
        self._quota_policy = quota_policy
        self._quota_settings = quota_settings
        self._admission_handle = admission_handle
        self._registry = active_stream_registry
        self._owner_runtime_id = owner_runtime_id
        self._limits = limits
        self._app_logger = app_logger
        self._started_at = started_at
        self._token = CancellationToken()
        self._condition = Condition(Lock())
        self._state: Literal[
            "created",
            "starting",
            "running",
            "closing",
            "closed",
        ] = "created"
        self._producer: Thread | None = None
        self._closing_thread: Thread | None = None
        self._sink: StreamEventSink | None = None
        self._model_session = None
        self._cancelled_model_session = None
        self._terminal_event: TurnCompleted | TurnFailed | None = None
        self._terminalizing = False
        self._cleanup_started = False
        self._delta_count = 0
        self._response_char_count = 0
        self._response_utf8_bytes = 0

    @property
    def _quota_enabled(self) -> bool:
        return (
            self._quota_persistence is not None
            and self._quota_policy is not None
            and self._quota_settings is not None
        )

    def __repr__(self) -> str:
        return (
            f"ChatStreamHandle(turn_id={self.turn_id!r}, "
            f"session_id={self.session_id!r})"
        )

    def start(self, *, sink: StreamEventSink) -> None:
        if sink is None:
            raise ValueError("stream_sink_required")
        start_error: BaseException | None = None
        with self._condition:
            if self._state != "created":
                raise RuntimeError("stream_handle_already_started_or_closed")
            self._sink = sink
            producer = Thread(
                target=self._run,
                name=f"aemeath-stream-{self.handle_id[-8:]}",
                daemon=False,
            )
            self._producer = producer
            self._state = "starting"
            try:
                producer.start()
            except BaseException as exc:
                start_error = exc
                self._producer = None
                self._state = "closing"
                self._closing_thread = current_thread()
                self._condition.notify_all()
            else:
                self._state = "running"
                self._condition.notify_all()
        if start_error is not None:
            self._token.request_cancel(reason="sse_generator_closed")
            self._terminalize_unstarted()
            raise start_error

    def request_cancel(self, *, reason: str) -> bool:
        """Atomically establish intent without touching the model transport."""
        return self._token.request_cancel(reason=reason)

    def cancel(self, *, reason: str) -> None:
        self.request_cancel(reason=reason)
        cancellation_reason = self._token.reason()
        if cancellation_reason is not None:
            self._cancel_model_session(reason=cancellation_reason)

    def close(self) -> None:
        caller = current_thread()
        close_unstarted = False
        with self._condition:
            if self._state == "closed":
                return
            if self._state == "closing":
                if self._closing_thread is caller:
                    return
                deadline = monotonic() + self._limits.producer_join_timeout_seconds
                while self._state != "closed":
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        return
                    self._condition.wait(timeout=remaining)
                return
            if self._state == "starting":
                raise RuntimeError("stream_handle_start_transition_incomplete")
            self._state = "closing"
            self._closing_thread = caller
            producer = self._producer
            close_unstarted = producer is None
        if close_unstarted:
            if not self._token.is_cancelled():
                self._token.request_cancel(reason="sse_generator_closed")
            self._terminalize_unstarted()
            return
        if producer is not None and producer is not caller:
            if not self._token.is_cancelled():
                self.cancel(reason="sse_generator_closed")
            producer.join(timeout=self._limits.producer_join_timeout_seconds)

    def _run(self) -> None:
        try:
            if self._outcome.kind == "replay_completed":
                self._run_completed_replay()
            elif self._outcome.kind == "replay_failed":
                self._run_failed_replay()
            else:
                self._run_reserved()
        except BaseException as exc:
            if self._outcome.kind in {"replay_completed", "replay_failed"}:
                self._record_replay_delivery_aborted(error=exc)
            else:
                self._terminalize_failure(error=exc, prepared=None)
        finally:
            self._cleanup()

    def _run_reserved(self) -> None:
        preflight = self._require_preflight()
        prepared: PreparedDialogueTurn | None = None
        answer_parts: list[str] = []
        try:
            self._raise_if_cancelled()
            prepared = self._turn_engine.prepare(
                preflight=preflight,
                user_message_id=self._outcome.record.user_message_id,
            )
            self._mark_provider_started(prepared=prepared)
            self._turn_engine.record_response_stream_started(preflight=preflight)
            finish_kind: str | None = None
            with self._model_client.open_stream(
                request=ModelStreamRequest(messages=prepared.prompt_messages)
            ) as model_session:
                with self._condition:
                    self._model_session = model_session
                cancellation_reason = self._token.reason()
                if cancellation_reason is not None:
                    self._cancel_model_session(reason=cancellation_reason)
                for model_event in model_session:
                    self._raise_if_cancelled()
                    if finish_kind is not None:
                        raise _model_invalid("model_stream_event_after_completion")
                    if isinstance(model_event, ModelTextDelta):
                        self._accept_delta(text=model_event.text, answer_parts=answer_parts)
                    elif isinstance(model_event, ModelStreamFinished):
                        if finish_kind is not None:
                            raise _model_invalid("model_stream_duplicate_completion")
                        finish_kind = model_event.finish_kind
                    else:
                        raise _model_invalid("model_stream_event_invalid")
                with self._condition:
                    self._model_session = None
            self._raise_if_cancelled()
            if finish_kind is None:
                raise _model_invalid("model_stream_completion_missing")
            answer = "".join(answer_parts)
            if not answer.strip():
                raise _model_invalid("model_response_blank")
            if finish_kind == "output_limit":
                preflight.warnings.append("model_output_truncated")
            completion_plan = self._turn_engine.decide_completion(
                prepared=prepared,
                assistant_response=answer,
            )
            self._raise_if_cancelled()
            record = self._complete_reserved_turn(
                assistant_response=answer,
                state_update=completion_plan.state_update,
                warnings=tuple(preflight.warnings),
            )
            if record.status != "completed":
                self._terminalize_from_record(record=record, prepared=prepared)
                return
            self._close_admission()
            self._run_post_commit_memory(
                prepared=prepared,
                memory_decision=completion_plan.memory_decision,
            )
            self._turn_engine.record_stream_completed(
                prepared=prepared,
                warnings=record.warnings,
                delta_count=self._delta_count,
                response_char_count=self._response_char_count,
                response_utf8_bytes=self._response_utf8_bytes,
            )
            self._deliver_terminal(_completed_event(record))
            self._record_terminal(status="completed", error_code=None)
        except BaseException as exc:
            self._terminalize_failure(error=exc, prepared=prepared)

    def _mark_provider_started(self, *, prepared: PreparedDialogueTurn) -> None:
        if not self._quota_enabled:
            return
        assert self._quota_persistence is not None
        assert self._quota_policy is not None
        assert self._quota_settings is not None
        prompt_utf8_bytes = prompt_messages_utf8_bytes(prepared.prompt_messages)
        prompt_estimate = self._quota_policy.estimate_prompt(
            request=UsagePromptEstimateRequest(
                estimator_version=self._quota_settings.estimator_version,
                prompt_messages_utf8_bytes=prompt_utf8_bytes,
                max_output_tokens=self._quota_settings.max_output_tokens,
            )
        )
        reservation = self._quota_persistence.mark_provider_started_with_quota(
            request=StreamQuotaProviderStarted(
                stream_request_id=self._outcome.record.id,
                turn_id=self.turn_id,
                user_id=self._outcome.record.user_id,
                session_id=self.session_id,
                owner_runtime_id=self._owner_runtime_id,
                prompt_estimate=prompt_estimate,
                retry_after_seconds=self._quota_settings.retry_after_seconds,
            )
        )
        if reservation.status == "released":
            self._record_quota_terminal(reservation=reservation)
            raise UsageQuotaExceededError(
                retry_after_seconds=self._quota_settings.retry_after_seconds,
                reason=reservation.terminal_reason or "daily_limit_reached",
            )
        self._safe_app_log(
            event_name="usage_quota.provider_started",
            level="info",
            error_code=None,
            payload=_quota_log_payload(reservation),
        )

    def _complete_reserved_turn(
        self,
        *,
        assistant_response: str,
        state_update,
        warnings: tuple[str, ...],
    ) -> StreamRequestRecord:
        if not self._quota_enabled:
            return self._persistence.complete(
                request=StreamTurnComplete(
                    record_id=self._outcome.record.id,
                    turn_id=self.turn_id,
                    user_id=self._outcome.record.user_id,
                    session_id=self.session_id,
                    owner_runtime_id=self._owner_runtime_id,
                    assistant_response=assistant_response,
                    state_update=state_update,
                    warnings=warnings,
                )
            )
        assert self._quota_persistence is not None
        assert self._quota_policy is not None
        assert self._quota_settings is not None
        output_estimate = self._quota_policy.estimate_output(
            request=UsageOutputEstimateRequest(
                estimator_version=self._quota_settings.estimator_version,
                response_utf8_bytes=self._response_utf8_bytes,
                delta_count=self._delta_count,
                max_output_tokens=self._quota_settings.max_output_tokens,
            )
        )
        terminal = self._quota_persistence.complete_with_quota(
            request=StreamQuotaComplete(
                stream_request_id=self._outcome.record.id,
                turn_id=self.turn_id,
                user_id=self._outcome.record.user_id,
                session_id=self.session_id,
                owner_runtime_id=self._owner_runtime_id,
                assistant_response=assistant_response,
                state_update=state_update,
                output_estimate=output_estimate,
                warnings=warnings,
            )
        )
        self._record_quota_terminal_from_result(terminal=terminal)
        return terminal.stream_record

    def _fail_reserved_turn(
        self,
        *,
        terminal_error_code: str,
        terminal_retryable: bool,
        terminal_reason: str,
    ) -> StreamRequestRecord:
        if not self._quota_enabled:
            return self._persistence.fail(
                request=StreamTurnFail(
                    record_id=self._outcome.record.id,
                    turn_id=self.turn_id,
                    user_id=self._outcome.record.user_id,
                    session_id=self.session_id,
                    owner_runtime_id=self._owner_runtime_id,
                    terminal_error_code=terminal_error_code,
                    terminal_retryable=terminal_retryable,
                    terminal_reason=terminal_reason,
                )
            )
        assert self._quota_persistence is not None
        terminal = self._quota_persistence.fail_with_quota(
            request=StreamQuotaFail(
                stream_request_id=self._outcome.record.id,
                turn_id=self.turn_id,
                user_id=self._outcome.record.user_id,
                session_id=self.session_id,
                owner_runtime_id=self._owner_runtime_id,
                terminal_error_code=terminal_error_code,
                terminal_retryable=terminal_retryable,
                terminal_reason=terminal_reason,
            )
        )
        self._record_quota_terminal_from_result(terminal=terminal)
        return terminal.stream_record

    def _record_quota_terminal_from_result(
        self,
        *,
        terminal: StreamQuotaTerminalRecord,
    ) -> None:
        if terminal.quota_reservation is not None:
            self._record_quota_terminal(reservation=terminal.quota_reservation)

    def _record_quota_terminal(
        self,
        *,
        reservation: StreamQuotaReservationRecord,
    ) -> None:
        if reservation.status == "settled":
            event_name = "usage_quota.settled"
        elif reservation.status == "released":
            event_name = "usage_quota.released"
        else:
            event_name = "usage_quota.reserved"
        self._safe_app_log(
            event_name=event_name,
            level="info",
            error_code=reservation.terminal_error_code,
            payload=_quota_log_payload(reservation),
        )

    def _accept_delta(self, *, text: str, answer_parts: list[str]) -> None:
        if not isinstance(text, str):
            raise _model_invalid("model_stream_delta_invalid")
        if not text:
            return
        try:
            encoded_length = len(text.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise _model_invalid("model_stream_delta_utf8_invalid") from exc
        next_chars = self._response_char_count + len(text)
        next_bytes = self._response_utf8_bytes + encoded_length
        if (
            next_chars > self._limits.response_max_chars
            or next_bytes > self._limits.response_max_utf8_bytes
        ):
            self._cancel_upstream(reason="stream_output_limit_exceeded")
            raise _model_invalid("stream_output_limit_exceeded")
        answer_parts.append(text)
        self._response_char_count = next_chars
        self._response_utf8_bytes = next_bytes
        for public_delta in _split_utf8(text, self._limits.public_delta_max_utf8_bytes):
            try:
                self._put_event(
                    ResponseDelta(event_type="response.delta", delta=public_delta)
                )
            except (StreamConsumerSlowError, TimeoutError) as exc:
                self.cancel(reason="consumer_slow")
                raise _cancelled_error("consumer_slow") from exc
            except Exception as exc:
                self.cancel(reason="sse_generator_closed")
                raise _cancelled_error("sse_generator_closed") from exc
            self._delta_count += 1

    def _run_completed_replay(self) -> None:
        assistant = self._outcome.assistant_message
        if assistant is None:
            raise RuntimeError("stream_completed_replay_missing_assistant")
        for public_delta in _split_utf8(
            assistant.content,
            self._limits.public_delta_max_utf8_bytes,
        ):
            self._raise_if_cancelled()
            self._put_event(ResponseDelta(event_type="response.delta", delta=public_delta))
            self._delta_count += 1
            self._response_char_count += len(public_delta)
            self._response_utf8_bytes += len(public_delta.encode("utf-8"))
        delivered = self._deliver_terminal(_completed_event(self._outcome.record))
        if delivered:
            self._record_terminal(status="replayed_completed", error_code=None)
        else:
            self._record_replay_delivery_aborted(
                error=RuntimeError("stream_terminal_delivery_failed")
            )

    def _run_failed_replay(self) -> None:
        self._raise_if_cancelled()
        error = _error_from_record(self._outcome.record)
        delivered = self._deliver_terminal(_failed_event(turn_id=self.turn_id, error=error))
        if delivered:
            self._record_terminal(status="replayed_failed", error_code=error.code.value)
        else:
            self._record_replay_delivery_aborted(
                error=RuntimeError("stream_terminal_delivery_failed")
            )

    def _record_replay_delivery_aborted(self, *, error: BaseException) -> None:
        cancellation_reason = self._token.reason()
        if cancellation_reason is not None:
            delivery_reason = cancellation_reason
        elif isinstance(error, (StreamConsumerSlowError, TimeoutError)):
            delivery_reason = "consumer_slow"
        else:
            delivery_reason = "delivery_failed"
        self._safe_app_log(
            event_name="agent_stream.replay_delivery_aborted",
            level="warning",
            error_code=self._outcome.record.terminal_error_code,
            payload={
                "delivery_status": "aborted",
                "delivery_reason": delivery_reason,
                "durable_status": self._outcome.record.status,
            },
        )

    def _terminalize_unstarted(self) -> None:
        try:
            if self.owns_in_progress_turn:
                self._terminalize_failure(
                    error=_cancelled_error(self._token.reason() or "sse_generator_closed"),
                    prepared=None,
                )
        finally:
            self._cleanup()

    def _terminalize_failure(
        self,
        *,
        error: BaseException,
        prepared: PreparedDialogueTurn | None,
    ) -> bool:
        with self._condition:
            if self._terminal_event is not None or self._terminalizing:
                return False
            self._terminalizing = True
        mapped_error = self._effective_error(error)
        record: StreamRequestRecord | None = None
        if self.owns_in_progress_turn:
            try:
                record = self._fail_reserved_turn(
                    terminal_error_code=mapped_error.code.value,
                    terminal_retryable=mapped_error.retryable,
                    terminal_reason=_persistence_reason(mapped_error),
                )
            except Exception:
                record = None
        if record is not None and record.status == "completed":
            if prepared is not None:
                self._turn_engine.record_stream_completed(
                    prepared=prepared,
                    warnings=record.warnings,
                    delta_count=self._delta_count,
                    response_char_count=self._response_char_count,
                    response_utf8_bytes=self._response_utf8_bytes,
                )
            self._deliver_terminal(_completed_event(record))
            self._record_terminal(status="completed", error_code=None)
            return True
        if record is not None and record.status == "failed":
            mapped_error = _error_from_record(record)
        preflight = self._preflight
        if preflight is not None:
            self._turn_engine.record_stream_failed(
                preflight=preflight,
                prepared=prepared,
                error=mapped_error,
                terminal_status=self._token.reason() or "failed",
                delta_count=self._delta_count,
                response_char_count=self._response_char_count,
                response_utf8_bytes=self._response_utf8_bytes,
            )
        self._deliver_terminal(_failed_event(turn_id=self.turn_id, error=mapped_error))
        self._record_terminal(status="failed", error_code=mapped_error.code.value)
        return True

    def _terminalize_from_record(
        self,
        *,
        record: StreamRequestRecord,
        prepared: PreparedDialogueTurn,
    ) -> None:
        if record.status == "completed":
            self._turn_engine.record_stream_completed(
                prepared=prepared,
                warnings=record.warnings,
                delta_count=self._delta_count,
                response_char_count=self._response_char_count,
                response_utf8_bytes=self._response_utf8_bytes,
            )
            self._deliver_terminal(_completed_event(record))
            self._record_terminal(status="completed", error_code=None)
            return
        error = _error_from_record(record)
        self._turn_engine.record_stream_failed(
            preflight=self._require_preflight(),
            prepared=prepared,
            error=error,
            terminal_status="failed",
            delta_count=self._delta_count,
            response_char_count=self._response_char_count,
            response_utf8_bytes=self._response_utf8_bytes,
        )
        self._deliver_terminal(_failed_event(turn_id=self.turn_id, error=error))
        self._record_terminal(status="failed", error_code=error.code.value)

    def _effective_error(self, error: BaseException) -> AemeathError:
        cancellation_reason = self._token.reason()
        if cancellation_reason is not None:
            return _cancelled_error(cancellation_reason)
        if isinstance(error, AemeathError):
            return error
        info = map_exception(error)
        return AemeathError(
            code=info.code,
            public_message=info.public_message,
            internal_message="stream_turn_failed",
            retryable=info.retryable,
            metadata={},
        )

    def _run_post_commit_memory(self, *, prepared, memory_decision) -> None:
        try:
            created_memory_id = self._turn_engine.memory_writer.persist_decision(
                user_id=self._outcome.record.user_id,
                decision=memory_decision,
                source_message_id=self._outcome.record.user_message_id,
                session_id=self.session_id,
                character_id=self._outcome.record.active_character_id,
            )
            window_memory_job_id = self._turn_engine.memory_writer.enqueue_window_if_due(
                user_id=self._outcome.record.user_id,
                session_id=self.session_id,
                character_id=self._outcome.record.active_character_id,
            )
            self._turn_engine.record_memory_write_decided(
                prepared=prepared,
                memory_decision=memory_decision,
                created_memory_id=created_memory_id,
                window_memory_job_id=window_memory_job_id,
            )
        except Exception as exc:
            self._safe_app_log(
                event_name="agent_stream.memory_post_commit_failed",
                level="error",
                error_code=ErrorCode.MEMORY_EXTRACTION_FAILED.value,
                payload={"error_type": type(exc).__name__},
            )

    def _raise_if_cancelled(self) -> None:
        reason = self._token.reason()
        if reason is not None:
            raise _cancelled_error(reason)

    def _cancel_upstream(self, *, reason: str) -> None:
        self._cancel_model_session(reason=reason)

    def _cancel_model_session(self, *, reason: str) -> None:
        with self._condition:
            model_session = self._model_session
            if model_session is None or self._cancelled_model_session is model_session:
                return
            self._cancelled_model_session = model_session
        if model_session is not None:
            try:
                model_session.cancel(reason=reason)
            except Exception:
                return

    def _put_event(self, event: ChatStreamEvent) -> None:
        with self._condition:
            sink = self._sink
            terminal_exists = self._terminal_event is not None
        if sink is None:
            raise RuntimeError("stream_sink_missing")
        if terminal_exists:
            raise RuntimeError("stream_event_after_terminal")
        sink.put_from_producer(
            event=event,
            timeout_seconds=self._limits.producer_put_timeout_seconds,
        )

    def _deliver_terminal(self, event: TurnCompleted | TurnFailed) -> bool:
        with self._condition:
            if self._terminal_event is not None:
                return False
            self._terminal_event = event
            self._terminalizing = False
            sink = self._sink
            self._condition.notify_all()
        if sink is None:
            return False
        try:
            sink.put_from_producer(
                event=event,
                timeout_seconds=self._limits.producer_put_timeout_seconds,
            )
        except Exception:
            return False
        return True

    def _close_admission(self) -> None:
        try:
            self._admission_handle.close()
        except Exception as exc:
            self._safe_app_log(
                event_name="runtime.stream_close_failed",
                level="error",
                error_code=None,
                payload={
                    "resource_name": "dialogue_admission",
                    "error_type": type(exc).__name__,
                },
            )

    def _cleanup(self) -> None:
        with self._condition:
            if self._cleanup_started:
                return
            self._cleanup_started = True
            sink = self._sink
            self._model_session = None
        self._close_admission()
        if sink is not None:
            try:
                sink.close_from_producer()
            except Exception:
                pass
        self._registry.deregister(handle_id=self.handle_id)
        with self._condition:
            self._state = "closed"
            self._closing_thread = None
            self._condition.notify_all()

    def _record_terminal(self, *, status: str, error_code: str | None) -> None:
        self._safe_app_log(
            event_name=(
                "agent_stream.completed" if error_code is None else "agent_stream.failed"
            ),
            level="info" if error_code is None else "error",
            error_code=error_code,
            payload={
                "status": status,
                "duration_ms": round((monotonic() - self._started_at) * 1000.0, 3),
                "delta_count": self._delta_count,
                "response_char_count": self._response_char_count,
                "response_utf8_bytes": self._response_utf8_bytes,
            },
        )

    def _safe_app_log(
        self,
        *,
        event_name: str,
        level: str,
        error_code: str | None,
        payload: dict[str, Any],
    ) -> None:
        try:
            self._app_logger.record(
                AppLogRecord(
                    event_name=event_name,
                    level=level,
                    request_id=self.request_id,
                    turn_id=self.turn_id,
                    session_id=self.session_id,
                    user_id=self._outcome.record.user_id,
                    character_id=self._outcome.record.active_character_id,
                    error_code=error_code,
                    payload=redact_sensitive(payload),
                )
            )
        except Exception:
            return

    def _require_preflight(self) -> TurnPreflight:
        if self._preflight is None:
            raise RuntimeError("stream_preflight_missing")
        return self._preflight


def _copy_json_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, list):
        return [_copy_json_value(item) for item in value]
    if isinstance(value, tuple):
        return [_copy_json_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _copy_json_value(item) for key, item in value.items()}
    raise ValueError("stream_metadata_invalid")


def _utc_quota_day() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _stream_outcome_from_quota(outcome: StreamQuotaResolveOutcome) -> StreamTurnResolveOutcome:
    if outcome.kind == "quota_exceeded" or outcome.record is None:
        raise RuntimeError("stream_quota_outcome_without_stream_record")
    return StreamTurnResolveOutcome(
        kind=outcome.kind,
        record=outcome.record,
        assistant_message=outcome.assistant_message,
    )


def _quota_log_payload(reservation: StreamQuotaReservationRecord) -> dict[str, Any]:
    return {
        "quota_day": reservation.quota_day,
        "surface": reservation.surface,
        "status": reservation.status,
        "charging_phase": reservation.charging_phase,
        "estimator_version": reservation.estimator_version,
        "reserved_turns": reservation.reserved_turns,
        "settled_turns": reservation.settled_turns,
        "reserved_tokens": reservation.reserved_tokens,
        "settled_tokens": reservation.settled_tokens,
        "reserved_input_tokens": reservation.reserved_input_tokens,
        "reserved_output_tokens": reservation.reserved_output_tokens,
        "prompt_messages_utf8_bytes": reservation.prompt_messages_utf8_bytes,
        "prompt_input_tokens": reservation.prompt_input_tokens,
        "output_utf8_bytes": reservation.output_utf8_bytes,
        "output_tokens": reservation.output_tokens,
        "delta_count": reservation.delta_count,
        "terminal_reason": reservation.terminal_reason,
    }


def _split_utf8(text: str, max_bytes: int) -> Iterator[str]:
    part: list[str] = []
    part_bytes = 0
    for character in text:
        encoded = character.encode("utf-8")
        if part and part_bytes + len(encoded) > max_bytes:
            yield "".join(part)
            part = []
            part_bytes = 0
        part.append(character)
        part_bytes += len(encoded)
    if part:
        yield "".join(part)


def _persistence_reason(error: AemeathError) -> str:
    if error.code == ErrorCode.CHAT_STREAM_CANCELLED:
        reason = error.metadata.get("reason")
        return reason if reason in _CANCELLATION_REASONS else "user_cancelled"
    if error.code == ErrorCode.MODEL_INVALID_RESPONSE:
        return "model_invalid_response"
    if error.code in _MODEL_ERROR_CODES:
        return "model_failed"
    if error.code == ErrorCode.SESSION_ARCHIVED:
        return "session_archived"
    if error.code == ErrorCode.SESSION_ENDED:
        return "session_ended"
    if error.code == ErrorCode.USAGE_QUOTA_EXCEEDED:
        return "preparation_failed"
    return "preparation_failed"


def _completed_event(record: StreamRequestRecord) -> TurnCompleted:
    return TurnCompleted(
        event_type="turn.completed",
        turn_id=record.turn_id,
        status="completed",
        warnings=record.warnings,
        state_delta=dict(record.state_delta),
    )


def _failed_event(*, turn_id: str, error: AemeathError) -> TurnFailed:
    info = error.to_error_info()
    return TurnFailed(
        event_type="turn.failed",
        turn_id=turn_id,
        error=PublicStreamError(
            code=info.code.value,
            message=info.public_message,
            retryable=info.retryable,
        ),
    )


def _error_from_record(record: StreamRequestRecord) -> AemeathError:
    try:
        code = ErrorCode(record.terminal_error_code or ErrorCode.INTERNAL_ERROR.value)
    except ValueError:
        code = ErrorCode.INTERNAL_ERROR
    return AemeathError(
        code=code,
        public_message=_public_message(code),
        internal_message="stream_terminal_replay",
        retryable=bool(record.terminal_retryable),
        metadata={"reason": record.terminal_reason},
    )


def _public_message(code: ErrorCode) -> str:
    messages = {
        ErrorCode.CHAT_STREAM_CANCELLED: "The streamed turn was cancelled.",
        ErrorCode.CHAT_STREAM_INTERRUPTED: "The streamed turn was interrupted.",
        ErrorCode.MODEL_INVALID_RESPONSE: "The model returned an invalid response.",
        ErrorCode.MODEL_TIMEOUT: "The model request timed out. Please try again.",
        ErrorCode.MODEL_RATE_LIMITED: "The model service is rate limited. Please try again later.",
        ErrorCode.MODEL_UNAVAILABLE: "The model service is temporarily unavailable.",
        ErrorCode.MODEL_BAD_REQUEST: "The model request was rejected.",
        ErrorCode.MODEL_AUTH_FAILED: "The model service rejected authentication.",
        ErrorCode.SESSION_ARCHIVED: "Session is archived and cannot be used.",
        ErrorCode.SESSION_ENDED: "Session has ended. Resume it before sending messages.",
        ErrorCode.USAGE_QUOTA_EXCEEDED: "Daily usage limit reached. Please try again later.",
    }
    return messages.get(code, "The streamed turn failed.")


def _cancelled_error(reason: str) -> AemeathError:
    return AemeathError(
        code=ErrorCode.CHAT_STREAM_CANCELLED,
        public_message="The streamed turn was cancelled.",
        internal_message="chat_stream_cancelled",
        retryable=False,
        metadata={"reason": reason},
    )


def _model_invalid(reason: str) -> AemeathError:
    return AemeathError(
        code=ErrorCode.MODEL_INVALID_RESPONSE,
        public_message="The model returned an invalid response.",
        internal_message=reason,
        retryable=False,
        metadata={"reason": reason},
    )


def _chat_request_error(*, conflict: bool) -> AemeathError:
    if conflict:
        return AemeathError(
            code=ErrorCode.CHAT_REQUEST_CONFLICT,
            public_message="The client message id conflicts with a different request.",
            internal_message="chat_request_conflict",
            retryable=False,
        )
    return AemeathError(
        code=ErrorCode.CHAT_REQUEST_IN_PROGRESS,
        public_message="This chat request is already in progress.",
        internal_message="chat_request_in_progress",
        retryable=True,
        metadata={"retry_after_seconds": 1},
    )


def _invalid_input(reason: str) -> AemeathError:
    return AemeathError(
        code=ErrorCode.INVALID_INPUT,
        public_message="Invalid input.",
        internal_message=reason,
        retryable=False,
        metadata={"reason": reason},
    )
