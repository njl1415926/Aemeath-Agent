from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    tags: list[str] = field(default_factory=list)
    requires_confirmation: bool = False


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    source: str = "model"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolContext:
    user_id: str
    session_id: str
    character_id: str
    response_mode: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolPolicyDecision:
    allowed: bool
    reason: str
    requires_confirmation: bool = False


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    name: str
    ok: bool
    content: str
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolNeedDecision:
    should_call: bool
    reason: str
    tool_calls: list[ToolCall] = field(default_factory=list)
