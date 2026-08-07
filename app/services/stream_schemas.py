from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, TypeAlias


@dataclass(frozen=True)
class StreamChatRequest:
    session_id: str
    character_id: str
    message: str = field(repr=False)
    client_message_id: str = field(repr=False)
    metadata: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class TurnStarted:
    event_type: Literal["turn.started"]
    request_id: str
    turn_id: str
    session_id: str


@dataclass(frozen=True)
class ResponseDelta:
    event_type: Literal["response.delta"]
    delta: str = field(repr=False)


@dataclass(frozen=True)
class TurnCompleted:
    event_type: Literal["turn.completed"]
    turn_id: str
    status: Literal["completed"]
    warnings: tuple[str, ...]
    state_delta: dict[str, int]


@dataclass(frozen=True)
class PublicStreamError:
    code: str
    message: str
    retryable: bool


@dataclass(frozen=True)
class TurnFailed:
    event_type: Literal["turn.failed"]
    turn_id: str
    error: PublicStreamError


ChatStreamEvent: TypeAlias = TurnStarted | ResponseDelta | TurnCompleted | TurnFailed


class StreamConsumerSlowError(TimeoutError):
    """A bounded producer put could not complete before its deadline."""


class StreamEventSink(Protocol):
    def put_from_producer(
        self,
        *,
        event: ChatStreamEvent,
        timeout_seconds: float,
    ) -> None: ...

    def close_from_producer(self) -> None: ...


class ChatStreamHandle(Protocol):
    request_id: str
    turn_id: str
    session_id: str
    started_event: TurnStarted

    def start(self, *, sink: StreamEventSink) -> None: ...

    def request_cancel(self, *, reason: str) -> bool: ...

    def cancel(self, *, reason: str) -> None: ...

    def close(self) -> None: ...


class AgentStreamServicePort(Protocol):
    def stream_chat(self, *, user_context, request: StreamChatRequest) -> ChatStreamHandle: ...
