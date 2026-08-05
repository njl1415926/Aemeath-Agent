from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Literal, Protocol

from app.schemas import MessageRecord, RelationshipState, StateUpdate

StreamRequestStatus = Literal["in_progress", "completed", "failed"]
StreamTurnResolveKind = Literal[
    "reserved",
    "in_progress",
    "conflict",
    "replay_completed",
    "replay_failed",
]

STREAM_TURN_TERMINAL_REASON_VALUES = (
    "user_cancelled",
    "client_disconnected",
    "sse_generator_closed",
    "consumer_slow",
    "runtime_closing",
    "model_failed",
    "model_invalid_response",
    "preparation_failed",
    "runtime_interrupted",
    "runtime_orphaned",
    "session_archived",
    "session_ended",
)
STREAM_TURN_TERMINAL_REASONS = frozenset(STREAM_TURN_TERMINAL_REASON_VALUES)

_STREAM_TURN_STATE_DELTA_KEYS = {"trust", "affection", "familiarity"}


@dataclass(frozen=True)
class StreamTurnResolveOrReserve:
    user_id: str
    session_id: str
    active_character_id: str
    client_message_id: str
    request_fingerprint_version: int
    request_fingerprint: str = field(repr=False)
    owner_runtime_id: str
    turn_id: str
    user_message_id: str
    user_message_content: str = field(repr=False)

    def __post_init__(self) -> None:
        _require_nonempty_strings(
            self.user_id,
            self.session_id,
            self.active_character_id,
            self.client_message_id,
            self.request_fingerprint,
            self.owner_runtime_id,
            self.turn_id,
            self.user_message_id,
        )
        if not isinstance(self.request_fingerprint_version, int) or isinstance(
            self.request_fingerprint_version, bool
        ) or self.request_fingerprint_version < 1:
            raise ValueError("stream_turn_fingerprint_version_invalid")
        if not isinstance(self.user_message_content, str):
            raise ValueError("stream_turn_user_message_invalid")


@dataclass(frozen=True)
class StreamTurnComplete:
    record_id: str
    turn_id: str
    user_id: str
    session_id: str
    owner_runtime_id: str
    assistant_response: str = field(repr=False)
    state_update: StateUpdate
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_nonempty_strings(
            self.record_id,
            self.turn_id,
            self.user_id,
            self.session_id,
            self.owner_runtime_id,
        )
        if not isinstance(self.assistant_response, str):
            raise ValueError("stream_turn_assistant_response_invalid")
        if not isinstance(self.state_update, StateUpdate):
            raise ValueError("stream_turn_state_update_invalid")
        if any(not isinstance(warning, str) for warning in self.warnings):
            raise ValueError("stream_turn_warning_invalid")


@dataclass(frozen=True)
class StreamTurnFail:
    record_id: str
    turn_id: str
    user_id: str
    session_id: str
    owner_runtime_id: str
    terminal_error_code: str
    terminal_retryable: bool
    terminal_reason: str

    def __post_init__(self) -> None:
        _require_nonempty_strings(
            self.record_id,
            self.turn_id,
            self.user_id,
            self.session_id,
            self.owner_runtime_id,
            self.terminal_error_code,
        )
        if not isinstance(self.terminal_retryable, bool):
            raise ValueError("stream_turn_terminal_retryable_invalid")
        if self.terminal_reason not in STREAM_TURN_TERMINAL_REASONS:
            raise ValueError("stream_turn_terminal_reason_invalid")


@dataclass(frozen=True)
class StreamRequestRecord:
    id: str
    user_id: str
    session_id: str
    client_message_id: str
    active_character_id: str
    request_fingerprint_version: int
    request_fingerprint: str = field(repr=False)
    turn_id: str
    user_message_id: str
    assistant_message_id: str | None
    status: StreamRequestStatus
    owner_runtime_id: str
    warnings: tuple[str, ...]
    state_delta: dict[str, int]
    terminal_error_code: str | None
    terminal_retryable: bool | None
    terminal_reason: str | None
    created_at: str
    updated_at: str
    completed_at: str | None
    failed_at: str | None


@dataclass(frozen=True)
class StreamTurnResolveOutcome:
    kind: StreamTurnResolveKind
    record: StreamRequestRecord
    assistant_message: MessageRecord | None = field(default=None, repr=False)


class StreamTurnPersistencePort(Protocol):
    def resolve_or_reserve(
        self,
        *,
        request: StreamTurnResolveOrReserve,
    ) -> StreamTurnResolveOutcome:
        ...

    def complete(self, *, request: StreamTurnComplete) -> StreamRequestRecord:
        ...

    def fail(self, *, request: StreamTurnFail) -> StreamRequestRecord:
        ...

    def fail_orphan_if_owned_by_other_runtime(
        self,
        *,
        owner_runtime_id: str,
        limit: int = 100,
    ) -> int:
        ...


def apply_stream_turn_state_update(
    *,
    current: RelationshipState | None,
    update: StateUpdate,
    updated_at: str,
) -> RelationshipState:
    """Apply the narrow completion-UoW relationship contract consistently."""

    base = current or RelationshipState()
    return RelationshipState(
        mood=update.aemeath_mood,
        user_emotion=update.user_emotion,
        trust=_clamp_stat(base.trust + update.trust_delta),
        affection=_clamp_stat(base.affection + update.affection_delta),
        familiarity=_clamp_stat(base.familiarity + update.familiarity_delta),
        current_scene=base.current_scene,
        last_topic=update.last_topic,
        updated_at=updated_at,
    )


def stream_turn_state_delta_json(update: StateUpdate) -> str:
    return json.dumps(
        {
            "affection": update.affection_delta,
            "familiarity": update.familiarity_delta,
            "trust": update.trust_delta,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def stream_turn_warnings_json(warnings: tuple[str, ...]) -> str:
    return json.dumps(list(warnings), ensure_ascii=True, separators=(",", ":"))


def parse_stream_turn_warnings(raw: str) -> tuple[str, ...]:
    value = json.loads(raw)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError("stream_turn_warnings_invalid")
    return tuple(value)


def parse_stream_turn_state_delta(
    raw: str,
    *,
    status: StreamRequestStatus,
) -> dict[str, int]:
    value = json.loads(raw)
    if not isinstance(value, dict) or set(value) - _STREAM_TURN_STATE_DELTA_KEYS:
        raise ValueError("stream_turn_state_delta_invalid")
    if any(not isinstance(item, int) or isinstance(item, bool) for item in value.values()):
        raise ValueError("stream_turn_state_delta_invalid")
    if status == "completed":
        if set(value) != _STREAM_TURN_STATE_DELTA_KEYS:
            raise ValueError("stream_turn_state_delta_invalid")
    elif value:
        raise ValueError("stream_turn_state_delta_invalid")
    return dict(value)


def _clamp_stat(value: int) -> int:
    return max(0, min(100, value))


def _require_nonempty_strings(*values: str) -> None:
    if any(not isinstance(value, str) or not value for value in values):
        raise ValueError("stream_turn_identifier_invalid")
