from __future__ import annotations

from typing import Protocol

from app.schemas import RelationshipState


class RelationshipStore(Protocol):
    def get_or_create_relationship_state(
        self,
        *,
        user_id: str,
        character_id: str,
    ) -> RelationshipState:
        ...

    def update_relationship_state(
        self,
        *,
        user_id: str,
        character_id: str,
        mood: str,
        user_emotion: str,
        trust_delta: int,
        affection_delta: int,
        familiarity_delta: int,
        last_topic: str | None,
    ) -> RelationshipState:
        ...
