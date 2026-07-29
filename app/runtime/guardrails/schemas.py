from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping


@dataclass(frozen=True)
class DialogueAdmissionRequest:
    user_id: str
    session_id: str
    request_id: str | None = None


@dataclass(frozen=True)
class DialogueAdmissionLease:
    session_wait_ms: float
    dialogue_wait_ms: float


@dataclass(frozen=True)
class ApiRateLimitRequest:
    user_id: str
    ip_key: str | None


@dataclass(frozen=True)
class ApiRateLimitDecision:
    allowed: bool
    reason_code: str
    retry_after_seconds: int = 0


@dataclass(frozen=True)
class ChatInputLimitRequest:
    message: str = field(repr=False)
    metadata: Mapping[str, object] = field(repr=False)


@dataclass(frozen=True)
class ChatInputLimitDecision:
    allowed: bool
    reason_code: str
    message_char_count: int
    message_utf8_bytes: int
    metadata_utf8_bytes: int | None
