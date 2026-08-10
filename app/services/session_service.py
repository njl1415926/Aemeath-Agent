from __future__ import annotations

from contextlib import contextmanager
from time import perf_counter
from typing import Any, Iterator

from app.agent.message_store import MessageStore
from app.errors import AemeathError, ErrorCode, map_exception, redact_sensitive
from app.observability.noop import NoopAppLogger
from app.observability.ports import AppLoggerPort
from app.observability.schemas import AppLogRecord
from app.runtime.guardrails.ports import DialogueAdmissionHandle, DialogueAdmissionPort
from app.runtime.guardrails.schemas import DialogueAdmissionLease, DialogueAdmissionRequest
from app.schemas import MessageRecord
from app.services.schemas import UserContext
from app.session.errors import (
    SessionArchivedError,
    SessionNotFoundError,
    SessionOwnershipError,
    SessionTransitionError,
)
from app.session.pagination import (
    Page,
    decode_cursor,
    encode_cursor,
    fingerprint_filters,
)
from app.session.ports import SessionStorePort
from app.session.schemas import SessionRecord, SessionStatus
from app.session.titles import normalize_session_title


class _NoopDialogueAdmissionHandle:
    def __init__(self) -> None:
        self._lease = DialogueAdmissionLease(session_wait_ms=0.0, dialogue_wait_ms=0.0)

    @property
    def lease(self) -> DialogueAdmissionLease:
        return self._lease

    def close(self) -> None:
        return None


class _NoopDialogueAdmission:
    def open_handle(
        self, *, request: DialogueAdmissionRequest
    ) -> DialogueAdmissionHandle:
        del request
        return _NoopDialogueAdmissionHandle()

    @contextmanager
    def acquire(
        self, *, request: DialogueAdmissionRequest
    ) -> Iterator[DialogueAdmissionLease]:
        handle = self.open_handle(request=request)
        try:
            yield handle.lease
        finally:
            handle.close()


class SessionService:
    def __init__(
        self,
        *,
        session_store: SessionStorePort,
        message_store: MessageStore,
        app_logger: AppLoggerPort | None = None,
        admission: DialogueAdmissionPort | None = None,
    ) -> None:
        self.session_store = session_store
        self.message_store = message_store
        self.app_logger = app_logger or NoopAppLogger()
        self.admission = admission or _NoopDialogueAdmission()

    def create_session(
        self,
        *,
        user_context: UserContext,
        character_id: str,
        title: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SessionRecord:
        started_at = perf_counter()
        try:
            normalized_title = normalize_session_title(title)
            session = self.session_store.create_session(
                user_id=user_context.user_id,
                character_id=character_id,
                title=normalized_title,
                metadata=metadata,
            )
            self._record(
                "api.session.created",
                user_context=user_context,
                session_id=session.id,
                character_id=session.character_id,
                payload={
                    "duration_ms": self._duration_ms(started_at),
                    "title_action": "cleared" if normalized_title is None else "set",
                    "title_length": len(normalized_title) if normalized_title else 0,
                },
            )
            return session
        except Exception as exc:
            self._record_failure(
                exc,
                user_context=user_context,
                operation="create",
                character_id=character_id,
                started_at=started_at,
            )
            raise

    def list_sessions(
        self,
        *,
        user_context: UserContext,
        character_id: str | None = None,
        status: SessionStatus | None = "active",
        limit: int = 50,
    ) -> list[SessionRecord]:
        return self.list_sessions_page(
            user_context=user_context,
            character_id=character_id,
            status=status,
            limit=limit,
            cursor=None,
        ).items

    def list_sessions_page(
        self,
        *,
        user_context: UserContext,
        character_id: str | None = None,
        status: SessionStatus | None = "active",
        limit: int = 50,
        cursor: str | None = None,
    ) -> Page[SessionRecord]:
        started_at = perf_counter()
        try:
            self._validate_limit(limit, minimum=1, maximum=200)
            filter_fingerprint = fingerprint_filters(
                kind="sessions",
                values={
                    "user_id": user_context.user_id,
                    "character_id": character_id,
                    "status": status if status is not None else "all",
                },
            )
            anchor = decode_cursor(
                cursor,
                kind="sessions",
                filter_fingerprint=filter_fingerprint,
            )
            rows = self.session_store.list_sessions_page(
                user_id=user_context.user_id,
                character_id=character_id,
                status=status,
                limit=limit + 1,
                after_updated_at=anchor.anchor_time if anchor else None,
                after_id=anchor.anchor_id if anchor else None,
            )
            returned = rows[:limit]
            has_more = len(rows) > limit
            next_cursor = None
            if has_more and returned:
                last = returned[-1]
                next_cursor = encode_cursor(
                    kind="sessions",
                    filter_fingerprint=filter_fingerprint,
                    anchor_time=last.updated_at,
                    anchor_id=last.id,
                )
            self._record(
                "api.session.listed",
                user_context=user_context,
                character_id=character_id,
                payload={
                    "duration_ms": self._duration_ms(started_at),
                    "limit": limit,
                    "has_more": has_more,
                    "cursor_present": cursor is not None,
                    "returned_count": len(returned),
                },
            )
            return Page(items=returned, next_cursor=next_cursor, has_more=has_more)
        except Exception as exc:
            self._record_failure(
                exc,
                user_context=user_context,
                operation="list_sessions",
                character_id=character_id,
                started_at=started_at,
                payload={"limit": limit, "cursor_present": cursor is not None},
            )
            raise

    def list_messages(
        self,
        *,
        user_context: UserContext,
        session_id: str,
        character_id: str | None = None,
        limit: int = 100,
    ) -> list[MessageRecord]:
        return self.list_messages_page(
            user_context=user_context,
            session_id=session_id,
            character_id=character_id,
            limit=limit,
            cursor=None,
        ).items

    def list_messages_page(
        self,
        *,
        user_context: UserContext,
        session_id: str,
        character_id: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> Page[MessageRecord]:
        started_at = perf_counter()
        checked_character_id = character_id
        try:
            self._validate_limit(limit, minimum=1, maximum=500)
            session = self.session_store.get_session(session_id=session_id)
            if session is None:
                raise SessionNotFoundError(f"session {session_id!r} was not found")
            if session.user_id != user_context.user_id:
                raise SessionOwnershipError(
                    f"session {session_id!r} belongs to a different user"
                )
            checked_character_id = character_id or session.character_id
            if session.character_id != checked_character_id:
                raise SessionOwnershipError(
                    f"session {session_id!r} belongs to a different character"
                )
            filter_fingerprint = fingerprint_filters(
                kind="messages",
                values={
                    "user_id": user_context.user_id,
                    "session_id": session_id,
                    "character_id": checked_character_id,
                },
            )
            anchor = decode_cursor(
                cursor,
                kind="messages",
                filter_fingerprint=filter_fingerprint,
            )
            rows = self.message_store.list_messages_by_session_page(
                session_id=session.id,
                limit=limit + 1,
                before_created_at=anchor.anchor_time if anchor else None,
                before_id=anchor.anchor_id if anchor else None,
            )
            returned_desc = rows[:limit]
            has_more = len(rows) > limit
            next_cursor = None
            if has_more and returned_desc:
                last_desc = returned_desc[-1]
                next_cursor = encode_cursor(
                    kind="messages",
                    filter_fingerprint=filter_fingerprint,
                    anchor_time=last_desc.created_at,
                    anchor_id=last_desc.id,
                )
            returned = list(reversed(returned_desc))
            self._record(
                "api.session.messages_listed",
                user_context=user_context,
                session_id=session_id,
                character_id=checked_character_id,
                payload={
                    "duration_ms": self._duration_ms(started_at),
                    "limit": limit,
                    "has_more": has_more,
                    "cursor_present": cursor is not None,
                    "returned_count": len(returned),
                },
            )
            return Page(items=returned, next_cursor=next_cursor, has_more=has_more)
        except Exception as exc:
            self._record_failure(
                exc,
                user_context=user_context,
                operation="list_messages",
                session_id=session_id,
                character_id=checked_character_id,
                started_at=started_at,
                payload={"limit": limit, "cursor_present": cursor is not None},
            )
            raise

    def update_title(
        self,
        *,
        user_context: UserContext,
        session_id: str,
        character_id: str,
        title: str | None,
    ) -> SessionRecord:
        started_at = perf_counter()
        try:
            self._require_owned_character_session(
                user_context=user_context,
                session_id=session_id,
                character_id=character_id,
                allow_archived=False,
            )
            normalized_title = normalize_session_title(title)
            updated = self.session_store.update_session_title(
                session_id=session_id,
                user_id=user_context.user_id,
                title=normalized_title,
            )
            self._record(
                "api.session.title_updated",
                user_context=user_context,
                session_id=session_id,
                character_id=character_id,
                payload={
                    "duration_ms": self._duration_ms(started_at),
                    "title_action": "cleared" if normalized_title is None else "set",
                    "title_length": len(normalized_title) if normalized_title else 0,
                },
            )
            return updated
        except Exception as exc:
            self._record_failure(
                exc,
                user_context=user_context,
                operation="update_title",
                session_id=session_id,
                character_id=character_id,
                started_at=started_at,
            )
            raise

    def end_session(
        self,
        *,
        user_context: UserContext,
        session_id: str,
        character_id: str,
    ) -> SessionRecord:
        started_at = perf_counter()
        failure_recorded = False
        try:
            with self.admission.acquire(
                request=DialogueAdmissionRequest(
                    user_id=user_context.user_id,
                    session_id=session_id,
                    request_id=user_context.request_id,
                )
            ):
                try:
                    session = self._require_owned_character_session(
                        user_context=user_context,
                        session_id=session_id,
                        character_id=character_id,
                        allow_archived=False,
                    )
                    updated = self.session_store.end_session(
                        session_id=session_id,
                        user_id=user_context.user_id,
                    )
                    self._record_lifecycle(
                        "api.session.ended",
                        user_context=user_context,
                        before=session,
                        after=updated,
                        duration_ms=self._duration_ms(started_at),
                    )
                    return updated
                except Exception as exc:
                    self._record_failure(
                        exc,
                        user_context=user_context,
                        operation="end",
                        session_id=session_id,
                        character_id=character_id,
                        started_at=started_at,
                    )
                    failure_recorded = True
                    raise
        except Exception as exc:
            if not failure_recorded:
                self._record_failure(
                    exc,
                    user_context=user_context,
                    operation="end",
                    session_id=session_id,
                    character_id=character_id,
                    started_at=started_at,
                )
            raise

    def resume_session(
        self,
        *,
        user_context: UserContext,
        session_id: str,
        character_id: str,
    ) -> SessionRecord:
        started_at = perf_counter()
        failure_recorded = False
        try:
            with self.admission.acquire(
                request=DialogueAdmissionRequest(
                    user_id=user_context.user_id,
                    session_id=session_id,
                    request_id=user_context.request_id,
                )
            ):
                try:
                    session = self._require_owned_character_session(
                        user_context=user_context,
                        session_id=session_id,
                        character_id=character_id,
                        allow_archived=False,
                    )
                    if session.status != "ended":
                        raise SessionTransitionError(
                            f"session {session_id!r} is not ended and cannot be resumed"
                        )
                    updated = self.session_store.resume_session(
                        session_id=session_id,
                        user_id=user_context.user_id,
                        character_id=character_id,
                    )
                    self._record_lifecycle(
                        "api.session.resumed",
                        user_context=user_context,
                        before=session,
                        after=updated,
                        duration_ms=self._duration_ms(started_at),
                    )
                    return updated
                except Exception as exc:
                    self._record_failure(
                        exc,
                        user_context=user_context,
                        operation="resume",
                        session_id=session_id,
                        character_id=character_id,
                        started_at=started_at,
                    )
                    failure_recorded = True
                    raise
        except Exception as exc:
            if not failure_recorded:
                self._record_failure(
                    exc,
                    user_context=user_context,
                    operation="resume",
                    session_id=session_id,
                    character_id=character_id,
                    started_at=started_at,
                )
            raise

    def archive_session(
        self,
        *,
        user_context: UserContext,
        session_id: str,
        character_id: str,
    ) -> SessionRecord:
        started_at = perf_counter()
        failure_recorded = False
        try:
            with self.admission.acquire(
                request=DialogueAdmissionRequest(
                    user_id=user_context.user_id,
                    session_id=session_id,
                    request_id=user_context.request_id,
                )
            ):
                try:
                    session = self._require_owned_character_session(
                        user_context=user_context,
                        session_id=session_id,
                        character_id=character_id,
                        allow_archived=True,
                    )
                    updated = self.session_store.archive_session(
                        session_id=session_id,
                        user_id=user_context.user_id,
                    )
                    self._record_lifecycle(
                        "api.session.archived",
                        user_context=user_context,
                        before=session,
                        after=updated,
                        duration_ms=self._duration_ms(started_at),
                    )
                    return updated
                except Exception as exc:
                    self._record_failure(
                        exc,
                        user_context=user_context,
                        operation="archive",
                        session_id=session_id,
                        character_id=character_id,
                        started_at=started_at,
                    )
                    failure_recorded = True
                    raise
        except Exception as exc:
            if not failure_recorded:
                self._record_failure(
                    exc,
                    user_context=user_context,
                    operation="archive",
                    session_id=session_id,
                    character_id=character_id,
                    started_at=started_at,
                )
            raise

    def _require_owned_character_session(
        self,
        *,
        user_context: UserContext,
        session_id: str,
        character_id: str,
        allow_archived: bool,
    ) -> SessionRecord:
        session = self.session_store.get_session(session_id=session_id)
        if session is None:
            raise SessionNotFoundError(f"session {session_id!r} was not found")
        if session.user_id != user_context.user_id:
            raise SessionOwnershipError(
                f"session {session_id!r} belongs to a different user"
            )
        if session.character_id != character_id:
            raise SessionOwnershipError(
                f"session {session_id!r} belongs to a different character"
            )
        if session.status == "archived" and not allow_archived:
            raise SessionArchivedError(f"session {session_id!r} is archived")
        return session

    def _record_lifecycle(
        self,
        event_name: str,
        *,
        user_context: UserContext,
        before: SessionRecord,
        after: SessionRecord,
        duration_ms: float,
    ) -> None:
        self._record(
            event_name,
            user_context=user_context,
            session_id=after.id,
            character_id=after.character_id,
            payload={
                "duration_ms": duration_ms,
                "status_before": before.status,
                "status_after": after.status,
            },
        )

    def _record_failure(
        self,
        exc: Exception,
        *,
        user_context: UserContext,
        operation: str,
        started_at: float,
        session_id: str | None = None,
        character_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        error_info = map_exception(exc)
        failure_payload = {
            "duration_ms": self._duration_ms(started_at),
            "operation": operation,
        }
        failure_payload.update(payload or {})
        cursor_reason = error_info.metadata.get("reason")
        if isinstance(cursor_reason, str):
            failure_payload["cursor_error_reason"] = cursor_reason
        self._record(
            "api.session.failed",
            user_context=user_context,
            session_id=session_id,
            character_id=character_id,
            payload=failure_payload,
            level="error",
            error_code=error_info.code.value,
        )

    def _record(
        self,
        event_name: str,
        *,
        user_context: UserContext,
        session_id: str | None = None,
        character_id: str | None = None,
        payload: dict[str, Any] | None = None,
        level: str = "info",
        error_code: str | None = None,
    ) -> None:
        try:
            self.app_logger.record(
                AppLogRecord(
                    event_name=event_name,
                    level=level,  # type: ignore[arg-type]
                    request_id=user_context.request_id,
                    user_id=user_context.user_id,
                    session_id=session_id,
                    character_id=character_id,
                    error_code=error_code,
                    payload=redact_sensitive(payload or {}),
                )
            )
        except Exception:
            return None

    @staticmethod
    def _duration_ms(started_at: float) -> float:
        return round((perf_counter() - started_at) * 1000, 3)

    @staticmethod
    def _validate_limit(limit: int, *, minimum: int, maximum: int) -> None:
        if not isinstance(limit, int) or isinstance(limit, bool):
            raise AemeathError(
                code=ErrorCode.INVALID_INPUT,
                public_message="Invalid pagination limit.",
                internal_message="pagination_limit_invalid_type",
            )
        if limit < minimum or limit > maximum:
            raise AemeathError(
                code=ErrorCode.INVALID_INPUT,
                public_message="Invalid pagination limit.",
                internal_message="pagination_limit_out_of_range",
                metadata={"minimum": minimum, "maximum": maximum},
            )
