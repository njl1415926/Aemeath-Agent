from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from app.utils.ids import new_id, utc_now

TraceStatus = Literal["started", "completed", "failed", "skipped"]
TraceLevel = Literal["debug", "info", "warning", "error"]
PromptRunStatus = Literal["completed", "failed"]


@dataclass
class ConversationRun:
    turn_id: str
    user_id: str
    session_id: str
    character_id: str
    started_at: str = field(default_factory=utc_now)
    ended_at: str | None = None
    status: TraceStatus = "started"
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class TraceEvent:
    event_type: str
    turn_id: str
    session_id: str
    user_id: str
    character_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    level: TraceLevel = "info"
    event_id: str = field(default_factory=lambda: new_id("trace"))
    created_at: str = field(default_factory=utc_now)


@dataclass(frozen=True)
class AppLogRecord:
    event_name: str
    level: TraceLevel = "info"
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)
    request_id: str | None = None
    turn_id: str | None = None
    session_id: str | None = None
    user_id: str | None = None
    character_id: str | None = None
    error_code: str | None = None


@dataclass(frozen=True)
class PromptRunRecord:
    run_id: str
    turn_id: str
    session_id: str
    user_id: str
    character_id: str
    model: str
    messages: list[dict[str, str]]
    prompt_context_metadata: dict[str, Any]
    sections: list[dict[str, Any]]
    response: str | None
    warnings: list[str]
    status: PromptRunStatus
    created_at: str = field(default_factory=utc_now)
    error_code: str | None = None
    error_type: str | None = None
    error_public_message: str | None = None
    error: str | None = None
