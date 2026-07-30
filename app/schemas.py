from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

MemoryOperation = Literal["create", "update", "merge", "archive", "skip"]


@dataclass
class AgentTurnInput:
    user_id: str
    session_id: str
    character_id: str
    message: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class UsedMemory:
    id: str
    content: str
    score: float | None
    match_reason: str


@dataclass
class AgentTurnOutput:
    turn_id: str
    session_id: str
    response: str
    used_memory_ids: list[str]
    state_delta: dict[str, int]
    warnings: list[str] = field(default_factory=list)
    used_memories: list[UsedMemory] = field(default_factory=list)


@dataclass
class MemoryDecision:
    should_write: bool
    operation: MemoryOperation
    namespace: str | None
    memory_type: str | None
    content: str | None
    importance: int | None
    confidence: float | None
    source: str | None
    source_message_ids: list[str] = field(default_factory=list)
    related_memory_ids: list[str] = field(default_factory=list)
    reason: str = ""
    expiry: str | None = None


@dataclass
class StateUpdate:
    user_emotion: str
    aemeath_mood: str
    trust_delta: int
    affection_delta: int
    familiarity_delta: int
    last_topic: str | None
    reason: str


@dataclass
class RelationshipState:
    mood: str = "calm"
    user_emotion: str = "neutral"
    trust: int = 50
    affection: int = 50
    familiarity: int = 50
    current_scene: str = "default_chat"
    last_topic: str | None = None
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class MessageRecord:
    id: str
    session_id: str
    user_id: str
    role: str
    content: str
    created_at: str
    emotion_label: str | None
    importance_score: int | None


@dataclass
class MemoryRecord:
    id: str
    user_id: str
    namespace: str
    memory_type: str
    content: str
    importance: int
    confidence: float
    source: str
    source_message_id: str | None
    related_memory_ids: str | None
    version: int
    status: str
    last_used_at: str | None
    use_count: int
    decay_score: float | None
    created_at: str
    updated_at: str | None
    expiry: str | None
    is_active: int
    scope: str = "user_global"
    scope_character_id: str | None = None
    scope_session_id: str | None = None
    source_session_id: str | None = None
    source_job_id: str | None = None
    source_message_ids: list[str] = field(default_factory=list)
