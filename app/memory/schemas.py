from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from app.schemas import MemoryRecord

MemoryNamespace = Literal[
    "user_profile",
    "interaction:user_profile",
    "interaction:preference",
    "interaction:project",
    "interaction:relationship",
]


@dataclass(frozen=True)
class MemoryQuery:
    user_id: str
    query_text: str
    character_id: str | None = None
    namespaces: list[str] = field(default_factory=list)
    memory_types: list[str] = field(default_factory=list)
    limit: int = 8
    min_confidence: float | None = None
    reason: str = "dialogue_context"


@dataclass(frozen=True)
class MemorySearchResult:
    record: MemoryRecord
    score: float
    match_reason: str
