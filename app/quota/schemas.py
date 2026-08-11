from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from app.db.stream_turns import STREAM_TURN_TERMINAL_REASONS, StreamRequestRecord
from app.schemas import MessageRecord, StateUpdate

ESTIMATOR_VERSION_PROMPT_UTF8_V0_1 = "prompt_utf8_v0_1"
UsageQuotaSurface = Literal["stream_chat"]
UsageQuotaReservationStatus = Literal["reserved", "settled", "released"]
UsageQuotaChargingPhase = Literal[
    "reserved_pre_provider",
    "provider_started",
    "terminalized",
]
StreamQuotaResolveKind = Literal[
    "reserved",
    "quota_exceeded",
    "in_progress",
    "conflict",
    "replay_completed",
    "replay_failed",
]


@dataclass(frozen=True)
class UsageQuotaPlanRequest:
    user_id: str
    quota_day: str
    surface: UsageQuotaSurface
    character_id: str
    turn_limit: int
    token_limit: int
    estimator_version: str
    context_budget_max_chars: int
    max_output_tokens: int

    def __post_init__(self) -> None:
        _require_nonempty_strings(
            self.user_id,
            self.quota_day,
            self.surface,
            self.character_id,
            self.estimator_version,
        )
        if self.surface != "stream_chat":
            raise ValueError("quota_surface_invalid")
        _require_positive_int(self.turn_limit, "quota_turn_limit_invalid")
        _require_positive_int(self.token_limit, "quota_token_limit_invalid")
        _require_nonnegative_int(
            self.context_budget_max_chars,
            "quota_context_budget_invalid",
        )
        _require_nonnegative_int(self.max_output_tokens, "quota_max_output_invalid")


@dataclass(frozen=True)
class UsageQuotaPlan:
    user_id: str
    quota_day: str
    surface: UsageQuotaSurface
    character_id: str
    turn_limit: int
    token_limit: int
    estimator_version: str
    context_budget_max_input_utf8_ceiling: int
    reserved_input_tokens: int
    reserved_output_tokens: int
    reserved_turns: int = 1

    @property
    def reserved_tokens(self) -> int:
        return self.reserved_input_tokens + self.reserved_output_tokens

    def __post_init__(self) -> None:
        _require_nonempty_strings(
            self.user_id,
            self.quota_day,
            self.surface,
            self.character_id,
            self.estimator_version,
        )
        if self.surface != "stream_chat":
            raise ValueError("quota_surface_invalid")
        _require_positive_int(self.turn_limit, "quota_turn_limit_invalid")
        _require_positive_int(self.token_limit, "quota_token_limit_invalid")
        _require_nonnegative_int(
            self.context_budget_max_input_utf8_ceiling,
            "quota_context_budget_invalid",
        )
        _require_nonnegative_int(self.reserved_input_tokens, "quota_reserved_input_invalid")
        _require_nonnegative_int(self.reserved_output_tokens, "quota_reserved_output_invalid")
        _require_positive_int(self.reserved_turns, "quota_reserved_turns_invalid")


@dataclass(frozen=True)
class UsagePromptEstimateRequest:
    estimator_version: str
    prompt_messages_utf8_bytes: int
    max_output_tokens: int

    def __post_init__(self) -> None:
        _require_nonempty_strings(self.estimator_version)
        _require_nonnegative_int(
            self.prompt_messages_utf8_bytes,
            "quota_prompt_bytes_invalid",
        )
        _require_nonnegative_int(self.max_output_tokens, "quota_max_output_invalid")


@dataclass(frozen=True)
class UsagePromptEstimate:
    estimator_version: str
    prompt_messages_utf8_bytes: int
    prompt_input_tokens: int
    reserved_output_tokens: int

    @property
    def reserved_tokens(self) -> int:
        return self.prompt_input_tokens + self.reserved_output_tokens


@dataclass(frozen=True)
class UsageOutputEstimateRequest:
    estimator_version: str
    response_utf8_bytes: int
    delta_count: int
    max_output_tokens: int

    def __post_init__(self) -> None:
        _require_nonempty_strings(self.estimator_version)
        _require_nonnegative_int(self.response_utf8_bytes, "quota_response_bytes_invalid")
        _require_nonnegative_int(self.delta_count, "quota_delta_count_invalid")
        _require_nonnegative_int(self.max_output_tokens, "quota_max_output_invalid")


@dataclass(frozen=True)
class UsageOutputEstimate:
    estimator_version: str
    response_utf8_bytes: int
    output_tokens: int
    delta_count: int


@dataclass(frozen=True)
class UsageQueryRequest:
    user_id: str
    quota_day: str

    def __post_init__(self) -> None:
        _require_nonempty_strings(self.user_id, self.quota_day)


@dataclass(frozen=True)
class UsageDailySummary:
    user_id: str
    quota_day: str
    turn_limit: int
    token_limit: int
    estimator_version: str
    reserved_turns: int
    settled_turns: int
    released_turns: int
    reserved_tokens: int
    settled_tokens: int
    released_tokens: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class StreamQuotaResolveOrReserve:
    stream_request: object
    quota_plan: UsageQuotaPlan
    retry_after_seconds: int

    def __post_init__(self) -> None:
        if not isinstance(self.quota_plan, UsageQuotaPlan):
            raise ValueError("quota_plan_invalid")
        _require_nonnegative_int(
            self.retry_after_seconds,
            "quota_retry_after_invalid",
        )


@dataclass(frozen=True)
class StreamQuotaReservationRecord:
    id: str
    user_id: str
    quota_day: str
    surface: UsageQuotaSurface
    idempotency_key: str
    session_id: str
    character_id: str
    stream_request_id: str
    turn_id: str | None
    owner_runtime_id: str
    status: UsageQuotaReservationStatus
    charging_phase: UsageQuotaChargingPhase
    reserved_turns: int
    reserved_tokens: int
    settled_turns: int
    settled_tokens: int
    estimator_version: str
    context_budget_max_input_utf8_ceiling: int
    reserved_input_tokens: int
    reserved_output_tokens: int
    prompt_messages_utf8_bytes: int
    prompt_input_tokens: int
    output_utf8_bytes: int
    output_tokens: int
    delta_count: int
    terminal_error_code: str | None
    terminal_reason: str | None
    created_at: str
    updated_at: str
    provider_started_at: str | None
    settled_at: str | None
    released_at: str | None


@dataclass(frozen=True)
class StreamQuotaResolveOutcome:
    kind: StreamQuotaResolveKind
    record: StreamRequestRecord | None = None
    quota_reservation: StreamQuotaReservationRecord | None = None
    daily_summary: UsageDailySummary | None = None
    assistant_message: MessageRecord | None = field(default=None, repr=False)
    retry_after_seconds: int | None = None


@dataclass(frozen=True)
class StreamQuotaProviderStarted:
    stream_request_id: str
    turn_id: str
    user_id: str
    session_id: str
    owner_runtime_id: str
    prompt_estimate: UsagePromptEstimate
    retry_after_seconds: int

    def __post_init__(self) -> None:
        _require_nonempty_strings(
            self.stream_request_id,
            self.turn_id,
            self.user_id,
            self.session_id,
            self.owner_runtime_id,
        )
        if not isinstance(self.prompt_estimate, UsagePromptEstimate):
            raise ValueError("quota_prompt_estimate_invalid")
        _require_nonnegative_int(
            self.retry_after_seconds,
            "quota_retry_after_invalid",
        )


@dataclass(frozen=True)
class StreamQuotaComplete:
    stream_request_id: str
    turn_id: str
    user_id: str
    session_id: str
    owner_runtime_id: str
    assistant_response: str = field(repr=False)
    state_update: StateUpdate
    output_estimate: UsageOutputEstimate
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_nonempty_strings(
            self.stream_request_id,
            self.turn_id,
            self.user_id,
            self.session_id,
            self.owner_runtime_id,
        )
        if not isinstance(self.assistant_response, str):
            raise ValueError("quota_assistant_response_invalid")
        if not isinstance(self.state_update, StateUpdate):
            raise ValueError("quota_state_update_invalid")
        if not isinstance(self.output_estimate, UsageOutputEstimate):
            raise ValueError("quota_output_estimate_invalid")
        if any(not isinstance(warning, str) for warning in self.warnings):
            raise ValueError("quota_warning_invalid")


@dataclass(frozen=True)
class StreamQuotaFail:
    stream_request_id: str
    turn_id: str
    user_id: str
    session_id: str
    owner_runtime_id: str
    terminal_error_code: str
    terminal_retryable: bool
    terminal_reason: str
    quota_terminal_reason: str | None = None

    def __post_init__(self) -> None:
        _require_nonempty_strings(
            self.stream_request_id,
            self.turn_id,
            self.user_id,
            self.session_id,
            self.owner_runtime_id,
            self.terminal_error_code,
            self.terminal_reason,
        )
        if not isinstance(self.terminal_retryable, bool):
            raise ValueError("quota_terminal_retryable_invalid")
        if self.terminal_reason not in STREAM_TURN_TERMINAL_REASONS:
            raise ValueError("quota_stream_terminal_reason_invalid")
        if self.quota_terminal_reason is not None:
            _require_nonempty_strings(self.quota_terminal_reason)


@dataclass(frozen=True)
class StreamQuotaTerminalRecord:
    stream_record: StreamRequestRecord
    quota_reservation: StreamQuotaReservationRecord | None
    daily_summary: UsageDailySummary | None


@dataclass(frozen=True)
class StreamQuotaRecoveryResult:
    recovered_count: int
    released_count: int
    settled_count: int


def _require_nonempty_strings(*values: str) -> None:
    if any(not isinstance(value, str) or not value for value in values):
        raise ValueError("quota_identifier_invalid")


def _require_nonnegative_int(value: int, reason: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(reason)


def _require_positive_int(value: int, reason: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(reason)
