from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

SessionStatus = Literal["active", "ended", "archived"]


@dataclass(frozen=True)
class SessionRecord:
    id: str
    user_id: str
    character_id: str
    status: SessionStatus
    title: str | None
    metadata: dict[str, Any]
    created_at: str
    updated_at: str
    ended_at: str | None
    archived_at: str | None
    last_message_at: str | None
    message_count: int
