from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from app.embeddings.schemas import ResolvedQueryEmbedding

SemanticQueryRole = Literal[
    "memory_recall",
    "canonical_lore",
    "canonical_voice",
    "character_examples",
]
SemanticRoleStatus = Literal["not_requested", "skipped", "ready", "failed"]
SemanticContextStatus = Literal["not_requested", "skipped", "ready", "failed"]
SemanticQueryStrategy = Literal[
    "none",
    "current_only",
    "recent_user_and_current",
    "recent_assistant_and_current",
    "last_turn_pair_and_current",
]

SEMANTIC_QUERY_ROLES: tuple[SemanticQueryRole, ...] = (
    "memory_recall",
    "canonical_lore",
    "canonical_voice",
    "character_examples",
)


@dataclass(frozen=True)
class SemanticMessage:
    id: str
    role: Literal["user", "assistant"]
    content: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id:
            raise ValueError("semantic message id must be a non-empty string")
        if self.role not in {"user", "assistant"}:
            raise ValueError("semantic message role must be user or assistant")
        if not isinstance(self.content, str):
            raise ValueError("semantic message content must be a string")


@dataclass(frozen=True)
class SemanticTurnInput:
    turn_id: str
    user_id: str
    session_id: str
    character_id: str
    current_user_message_id: str
    current_input: str = field(repr=False)
    recent_messages: tuple[SemanticMessage, ...] = field(repr=False)


@dataclass(frozen=True)
class SemanticQueryRequest:
    role: SemanticQueryRole
    construction_version: str
    budget_version: str
    query_strategy: SemanticQueryStrategy
    canonical_query: str = field(repr=False)
    dependency_message_ids: tuple[str, ...] = field(default=(), repr=False)


@dataclass(frozen=True)
class SemanticQueryResolution:
    role: SemanticQueryRole
    status: SemanticRoleStatus
    reason_code: str
    request_key: str | None
    resolved_embedding: ResolvedQueryEmbedding | None = field(repr=False)
    error_code: str | None = None


@dataclass(frozen=True)
class TurnSemanticContext:
    turn_id: str
    status: SemanticContextStatus
    construction_version: str
    budget_version: str
    query_strategy: SemanticQueryStrategy
    budget_reason_codes: tuple[str, ...]
    provider: str | None
    profile_id: str | None
    signature_fingerprint: str | None
    requested_role_count: int
    unique_query_count: int
    dependency_message_count: int
    query_char_count: int
    query_utf8_bytes: int
    provider_called: bool
    duration_ms: float
    resolutions: tuple[SemanticQueryResolution, ...] = field(repr=False)

    @property
    def deduplicated_role_count(self) -> int:
        return max(0, self.requested_role_count - self.unique_query_count)

    def resolution_for(self, role: SemanticQueryRole) -> SemanticQueryResolution:
        for resolution in self.resolutions:
            if resolution.role == role:
                return resolution
        raise KeyError(role)

    def resolved_for(self, role: SemanticQueryRole) -> ResolvedQueryEmbedding | None:
        return self.resolution_for(role).resolved_embedding

    def safe_summary(self) -> dict[str, object]:
        return {
            "turn_id": self.turn_id,
            "construction_version": self.construction_version,
            "budget_version": self.budget_version,
            "context_status": self.status,
            "query_strategy": self.query_strategy,
            "budget_reason_codes": list(self.budget_reason_codes),
            "roles": [
                {
                    "role": resolution.role,
                    "status": resolution.status,
                    "reason_code": resolution.reason_code,
                    "error_code": resolution.error_code,
                }
                for resolution in self.resolutions
            ],
            "requested_role_count": self.requested_role_count,
            "unique_query_count": self.unique_query_count,
            "deduplicated_role_count": self.deduplicated_role_count,
            "dependency_message_count": self.dependency_message_count,
            "query_char_count": self.query_char_count,
            "query_utf8_bytes": self.query_utf8_bytes,
            "provider_called": self.provider_called,
            "provider": self.provider,
            "profile_id": self.profile_id,
            "signature_fingerprint": self.signature_fingerprint,
            "duration_ms": round(max(0.0, self.duration_ms), 3),
        }
