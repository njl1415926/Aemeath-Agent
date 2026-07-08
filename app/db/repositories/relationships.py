from __future__ import annotations

from app.db.models import RelationshipStateModel
from app.db.repositories.common import SessionFactory, clamp_stat
from app.schemas import RelationshipState
from app.utils.ids import new_id, utc_now


class RelationshipRepository:
    def __init__(self, session_factory: SessionFactory):
        self._session_factory = session_factory

    def _from_model(self, model: RelationshipStateModel) -> RelationshipState:
        return RelationshipState(
            mood=model.mood or "calm",
            user_emotion=model.user_emotion or "neutral",
            trust=model.trust if model.trust is not None else 50,
            affection=model.affection if model.affection is not None else 50,
            familiarity=model.familiarity if model.familiarity is not None else 50,
            current_scene=model.current_scene or "default_chat",
            last_topic=model.last_topic,
            updated_at=model.updated_at,
        )

    def get_or_create_relationship_state(
        self,
        *,
        user_id: str,
        character_id: str,
    ) -> RelationshipState:
        with self._session_factory() as session:
            model = (
                session.query(RelationshipStateModel)
                .filter(
                    RelationshipStateModel.user_id == user_id,
                    RelationshipStateModel.character_id == character_id,
                )
                .limit(1)
                .one_or_none()
            )
            if model is not None:
                return self._from_model(model)
            state = RelationshipState()
            session.add(
                RelationshipStateModel(
                    id=new_id("relation"),
                    user_id=user_id,
                    character_id=character_id,
                    mood=state.mood,
                    user_emotion=state.user_emotion,
                    trust=state.trust,
                    affection=state.affection,
                    familiarity=state.familiarity,
                    current_scene=state.current_scene,
                    last_topic=state.last_topic,
                    updated_at=state.updated_at,
                )
            )
            session.commit()
            return state

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
        current = self.get_or_create_relationship_state(
            user_id=user_id,
            character_id=character_id,
        )
        updated = RelationshipState(
            mood=mood,
            user_emotion=user_emotion,
            trust=clamp_stat(current.trust + trust_delta),
            affection=clamp_stat(current.affection + affection_delta),
            familiarity=clamp_stat(current.familiarity + familiarity_delta),
            current_scene=current.current_scene,
            last_topic=last_topic,
            updated_at=utc_now(),
        )
        with self._session_factory() as session:
            model = (
                session.query(RelationshipStateModel)
                .filter(
                    RelationshipStateModel.user_id == user_id,
                    RelationshipStateModel.character_id == character_id,
                )
                .limit(1)
                .one()
            )
            model.mood = updated.mood
            model.user_emotion = updated.user_emotion
            model.trust = updated.trust
            model.affection = updated.affection
            model.familiarity = updated.familiarity
            model.current_scene = updated.current_scene
            model.last_topic = updated.last_topic
            model.updated_at = updated.updated_at
            session.commit()
        return updated
