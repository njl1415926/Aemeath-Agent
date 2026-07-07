from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class UserContext:
    user_id: str
    roles: list[str] = field(default_factory=list)
    request_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ChatRequest:
    session_id: str
    character_id: str
    message: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class UsedMemorySummary:
    id: str
    content: str
    score: float | None
    match_reason: str


@dataclass(frozen=True)
class ChatResponse:
    turn_id: str
    session_id: str
    character_id: str
    response: str
    warnings: list[str]
    used_memory_ids: list[str]
    state_delta: dict[str, int]
    used_memories: list[UsedMemorySummary] = field(default_factory=list)
