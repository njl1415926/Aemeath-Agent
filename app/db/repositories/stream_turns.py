from __future__ import annotations

from app.db.errors import StorageMigrationRequiredError
from app.db.models import (
    ChatRequestIdempotencyModel,
    MessageModel,
    RelationshipStateModel,
    SessionModel,
)
from app.db.repositories.common import SessionFactory
from app.db.stream_turns import (
    StreamRequestRecord,
    StreamTurnComplete,
    StreamTurnFail,
    StreamTurnResolveOrReserve,
    StreamTurnResolveOutcome,
    apply_stream_turn_state_update,
    parse_stream_turn_state_delta,
    parse_stream_turn_warnings,
    stream_turn_state_delta_json,
    stream_turn_warnings_json,
)
from app.schemas import MessageRecord, RelationshipState
from app.session.errors import (
    SessionArchivedError,
    SessionEndedError,
    SessionNotFoundError,
    SessionOwnershipError,
)
from app.utils.ids import new_id, utc_now


class StreamTurnRepository:
    """SQLite-backed narrow UoW for durable streamed dialogue turns."""

    def __init__(self, session_factory: SessionFactory):
        self._session_factory = session_factory

    def resolve_or_reserve(
        self,
        *,
        request: StreamTurnResolveOrReserve,
    ) -> StreamTurnResolveOutcome:
        with self._session_factory() as session:
            try:
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                session_model = self._require_owned_session(
                    session,
                    session_id=request.session_id,
                    user_id=request.user_id,
                    active_character_id=request.active_character_id,
                )
                if session_model.status == "archived":
                    raise SessionArchivedError("stream_turn_session_archived")

                record_model = self._find_by_scope(
                    session,
                    user_id=request.user_id,
                    session_id=request.session_id,
                    client_message_id=request.client_message_id,
                )
                if session_model.status == "ended":
                    if (
                        record_model is not None
                        and self._matches_fingerprint(record_model, request)
                        and record_model.status in {"completed", "failed"}
                    ):
                        record = self._record_from_model(record_model)
                        outcome = self._replay_outcome(session, record)
                    else:
                        raise SessionEndedError("stream_turn_session_ended")
                elif record_model is None:
                    outcome = self._reserve(session, request=request)
                elif not self._matches_fingerprint(record_model, request):
                    outcome = StreamTurnResolveOutcome(
                        kind="conflict",
                        record=self._record_from_model(record_model),
                    )
                elif record_model.status == "in_progress":
                    if record_model.owner_runtime_id != request.owner_runtime_id:
                        self._mark_failed(
                            record_model,
                            error_code="CHAT_STREAM_INTERRUPTED",
                            retryable=False,
                            reason="runtime_orphaned",
                        )
                        outcome = StreamTurnResolveOutcome(
                            kind="replay_failed",
                            record=self._record_from_model(record_model),
                        )
                    else:
                        outcome = StreamTurnResolveOutcome(
                            kind="in_progress",
                            record=self._record_from_model(record_model),
                        )
                else:
                    outcome = self._replay_outcome(
                        session,
                        self._record_from_model(record_model),
                    )
                session.commit()
                return outcome
            except BaseException:
                session.rollback()
                raise

    def complete(self, *, request: StreamTurnComplete) -> StreamRequestRecord:
        if not request.assistant_response.strip():
            raise ValueError("stream_turn_assistant_response_blank")
        with self._session_factory() as session:
            try:
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                record_model = self._require_record(
                    session,
                    record_id=request.record_id,
                    turn_id=request.turn_id,
                    user_id=request.user_id,
                    session_id=request.session_id,
                )
                if (
                    record_model.status != "in_progress"
                    or record_model.owner_runtime_id != request.owner_runtime_id
                ):
                    result = self._record_from_model(record_model)
                else:
                    session_model = self._require_owned_session(
                        session,
                        session_id=request.session_id,
                        user_id=request.user_id,
                        active_character_id=record_model.active_character_id,
                    )
                    if session_model.status != "active":
                        self._mark_failed(
                            record_model,
                            error_code=(
                                "SESSION_ARCHIVED"
                                if session_model.status == "archived"
                                else "SESSION_ENDED"
                            ),
                            retryable=False,
                            reason=(
                                "session_archived"
                                if session_model.status == "archived"
                                else "session_ended"
                            ),
                        )
                    else:
                        self._complete_active_record(
                            session,
                            record_model=record_model,
                            request=request,
                        )
                    session.flush()
                    result = self._record_from_model(record_model)
                session.commit()
                return result
            except BaseException:
                session.rollback()
                raise

    def fail(self, *, request: StreamTurnFail) -> StreamRequestRecord:
        with self._session_factory() as session:
            try:
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                record_model = self._require_record(
                    session,
                    record_id=request.record_id,
                    turn_id=request.turn_id,
                    user_id=request.user_id,
                    session_id=request.session_id,
                )
                if (
                    record_model.status == "in_progress"
                    and record_model.owner_runtime_id == request.owner_runtime_id
                ):
                    self._mark_failed(
                        record_model,
                        error_code=request.terminal_error_code,
                        retryable=request.terminal_retryable,
                        reason=request.terminal_reason,
                    )
                    session.flush()
                result = self._record_from_model(record_model)
                session.commit()
                return result
            except BaseException:
                session.rollback()
                raise

    def fail_orphan_if_owned_by_other_runtime(
        self,
        *,
        owner_runtime_id: str,
        limit: int = 100,
    ) -> int:
        if not isinstance(owner_runtime_id, str) or not owner_runtime_id:
            raise ValueError("stream_turn_owner_runtime_id_invalid")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
            raise ValueError("stream_turn_orphan_limit_invalid")
        with self._session_factory() as session:
            try:
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                records = (
                    session.query(ChatRequestIdempotencyModel)
                    .filter(ChatRequestIdempotencyModel.status == "in_progress")
                    .filter(ChatRequestIdempotencyModel.owner_runtime_id != owner_runtime_id)
                    .order_by(
                        ChatRequestIdempotencyModel.updated_at,
                        ChatRequestIdempotencyModel.id,
                    )
                    .limit(limit)
                    .all()
                )
                for record in records:
                    self._mark_failed(
                        record,
                        error_code="CHAT_STREAM_INTERRUPTED",
                        retryable=False,
                        reason="runtime_orphaned",
                    )
                session.flush()
                session.commit()
                return len(records)
            except BaseException:
                session.rollback()
                raise

    def _reserve(
        self,
        session,
        *,
        request: StreamTurnResolveOrReserve,
    ) -> StreamTurnResolveOutcome:
        now = utc_now()
        session.add(
            MessageModel(
                id=request.user_message_id,
                session_id=request.session_id,
                user_id=request.user_id,
                role="user",
                content=request.user_message_content,
                created_at=now,
                emotion_label=None,
                importance_score=None,
            )
        )
        # The ORM has no relationship edge between these models, so establish
        # the message FK target explicitly before flushing the request record.
        session.flush()
        record_model = ChatRequestIdempotencyModel(
            id=new_id("stream_request"),
            user_id=request.user_id,
            session_id=request.session_id,
            client_message_id=request.client_message_id,
            active_character_id=request.active_character_id,
            request_fingerprint_version=request.request_fingerprint_version,
            request_fingerprint=request.request_fingerprint,
            turn_id=request.turn_id,
            user_message_id=request.user_message_id,
            assistant_message_id=None,
            status="in_progress",
            owner_runtime_id=request.owner_runtime_id,
            warnings_json="[]",
            state_delta_json="{}",
            terminal_error_code=None,
            terminal_retryable=None,
            terminal_reason=None,
            created_at=now,
            updated_at=now,
            completed_at=None,
            failed_at=None,
        )
        session.add(record_model)
        session.flush()
        return StreamTurnResolveOutcome(
            kind="reserved",
            record=self._record_from_model(record_model),
        )

    def _complete_active_record(
        self,
        session,
        *,
        record_model,
        request: StreamTurnComplete,
    ) -> None:
        now = utc_now()
        assistant_message_id = new_id("message")
        session.add(
            MessageModel(
                id=assistant_message_id,
                session_id=record_model.session_id,
                user_id=record_model.user_id,
                role="assistant",
                content=request.assistant_response,
                created_at=now,
                emotion_label=None,
                importance_score=None,
            )
        )

        relationship_model = (
            session.query(RelationshipStateModel)
            .filter(RelationshipStateModel.user_id == record_model.user_id)
            .filter(
                RelationshipStateModel.character_id == record_model.active_character_id
            )
            .one_or_none()
        )
        current = self._relationship_from_model(relationship_model)
        updated = apply_stream_turn_state_update(
            current=current,
            update=request.state_update,
            updated_at=now,
        )
        if relationship_model is None:
            relationship_model = RelationshipStateModel(
                id=new_id("relation"),
                user_id=record_model.user_id,
                character_id=record_model.active_character_id,
                mood=updated.mood,
                user_emotion=updated.user_emotion,
                trust=updated.trust,
                affection=updated.affection,
                familiarity=updated.familiarity,
                current_scene=updated.current_scene,
                last_topic=updated.last_topic,
                updated_at=updated.updated_at,
            )
            session.add(relationship_model)
        else:
            relationship_model.mood = updated.mood
            relationship_model.user_emotion = updated.user_emotion
            relationship_model.trust = updated.trust
            relationship_model.affection = updated.affection
            relationship_model.familiarity = updated.familiarity
            relationship_model.current_scene = updated.current_scene
            relationship_model.last_topic = updated.last_topic
            relationship_model.updated_at = updated.updated_at

        record_model.assistant_message_id = assistant_message_id
        record_model.status = "completed"
        record_model.warnings_json = stream_turn_warnings_json(request.warnings)
        record_model.state_delta_json = stream_turn_state_delta_json(request.state_update)
        record_model.terminal_error_code = None
        record_model.terminal_retryable = None
        record_model.terminal_reason = None
        record_model.updated_at = now
        record_model.completed_at = now
        record_model.failed_at = None

    def _find_by_scope(self, session, *, user_id: str, session_id: str, client_message_id: str):
        return (
            session.query(ChatRequestIdempotencyModel)
            .filter(ChatRequestIdempotencyModel.user_id == user_id)
            .filter(ChatRequestIdempotencyModel.session_id == session_id)
            .filter(ChatRequestIdempotencyModel.client_message_id == client_message_id)
            .one_or_none()
        )

    def _require_record(
        self,
        session,
        *,
        record_id: str,
        turn_id: str,
        user_id: str,
        session_id: str,
    ):
        record = (
            session.query(ChatRequestIdempotencyModel)
            .filter(ChatRequestIdempotencyModel.id == record_id)
            .filter(ChatRequestIdempotencyModel.turn_id == turn_id)
            .filter(ChatRequestIdempotencyModel.user_id == user_id)
            .filter(ChatRequestIdempotencyModel.session_id == session_id)
            .one_or_none()
        )
        if record is None:
            raise StorageMigrationRequiredError(reason="streaming_chat_record_not_found")
        return record

    def _require_owned_session(
        self,
        session,
        *,
        session_id: str,
        user_id: str,
        active_character_id: str,
    ) -> SessionModel:
        session_model = session.get(SessionModel, session_id)
        if session_model is None:
            raise SessionNotFoundError("stream_turn_session_not_found")
        if (
            session_model.user_id != user_id
            or session_model.character_id != active_character_id
        ):
            raise SessionOwnershipError("stream_turn_session_ownership_invalid")
        return session_model

    def _replay_outcome(self, session, record: StreamRequestRecord) -> StreamTurnResolveOutcome:
        if record.status == "failed":
            return StreamTurnResolveOutcome(kind="replay_failed", record=record)
        if record.status != "completed" or record.assistant_message_id is None:
            raise StorageMigrationRequiredError(reason="streaming_chat_replay_record_invalid")
        assistant = session.get(MessageModel, record.assistant_message_id)
        if (
            assistant is None
            or assistant.session_id != record.session_id
            or assistant.user_id != record.user_id
            or assistant.role != "assistant"
        ):
            raise StorageMigrationRequiredError(reason="streaming_chat_replay_assistant_invalid")
        return StreamTurnResolveOutcome(
            kind="replay_completed",
            record=record,
            assistant_message=self._message_from_model(assistant),
        )

    def _matches_fingerprint(
        self,
        record: ChatRequestIdempotencyModel,
        request: StreamTurnResolveOrReserve,
    ) -> bool:
        return (
            record.request_fingerprint_version == request.request_fingerprint_version
            and record.request_fingerprint == request.request_fingerprint
        )

    def _mark_failed(self, record_model, *, error_code: str, retryable: bool, reason: str) -> None:
        now = utc_now()
        record_model.assistant_message_id = None
        record_model.status = "failed"
        record_model.warnings_json = "[]"
        record_model.state_delta_json = "{}"
        record_model.terminal_error_code = error_code
        record_model.terminal_retryable = int(retryable)
        record_model.terminal_reason = reason
        record_model.updated_at = now
        record_model.completed_at = None
        record_model.failed_at = now

    def _record_from_model(self, model: ChatRequestIdempotencyModel) -> StreamRequestRecord:
        status = model.status
        if status not in {"in_progress", "completed", "failed"}:
            raise StorageMigrationRequiredError(reason="streaming_chat_record_status_invalid")
        try:
            warnings = parse_stream_turn_warnings(model.warnings_json)
            state_delta = parse_stream_turn_state_delta(
                model.state_delta_json,
                status=status,
            )
        except (TypeError, ValueError) as exc:
            raise StorageMigrationRequiredError(
                reason="streaming_chat_record_payload_invalid"
            ) from exc
        if model.terminal_retryable not in {None, 0, 1}:
            raise StorageMigrationRequiredError(reason="streaming_chat_record_terminal_invalid")
        return StreamRequestRecord(
            id=model.id,
            user_id=model.user_id,
            session_id=model.session_id,
            client_message_id=model.client_message_id,
            active_character_id=model.active_character_id,
            request_fingerprint_version=model.request_fingerprint_version,
            request_fingerprint=model.request_fingerprint,
            turn_id=model.turn_id,
            user_message_id=model.user_message_id,
            assistant_message_id=model.assistant_message_id,
            status=status,
            owner_runtime_id=model.owner_runtime_id,
            warnings=warnings,
            state_delta=state_delta,
            terminal_error_code=model.terminal_error_code,
            terminal_retryable=(
                bool(model.terminal_retryable)
                if model.terminal_retryable is not None
                else None
            ),
            terminal_reason=model.terminal_reason,
            created_at=model.created_at,
            updated_at=model.updated_at,
            completed_at=model.completed_at,
            failed_at=model.failed_at,
        )

    def _relationship_from_model(
        self,
        model: RelationshipStateModel | None,
    ) -> RelationshipState | None:
        if model is None:
            return None
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

    def _message_from_model(self, model: MessageModel) -> MessageRecord:
        return MessageRecord(
            id=model.id,
            session_id=model.session_id,
            user_id=model.user_id,
            role=model.role,
            content=model.content,
            created_at=model.created_at,
            emotion_label=model.emotion_label,
            importance_score=model.importance_score,
        )
