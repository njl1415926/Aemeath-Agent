from __future__ import annotations

import json
from dataclasses import asdict

import pytest

from app.errors import ErrorCode, map_exception
from app.main import build_agent_service
from app.observability.schemas import AppLogRecord
from app.services.agent_service import AgentService
from app.services.schemas import ChatRequest, UserContext
from app.session.errors import SessionArchivedError, SessionEndedError
from app.utils.ids import new_id


class _RecordingAppLogger:
    def __init__(self) -> None:
        self.records: list[AppLogRecord] = []

    def record(self, record: AppLogRecord) -> None:
        self.records.append(record)


class _FailingController:
    def handle_user_message(self, turn_input):
        raise ValueError(
            "provider failed token=secret123 api-key=secret456 "
            "authorization=Bearer auth-secret cookie=session-secret"
        )


def test_agent_service_successful_chat(settings):
    service, _memory_admin, resolved_settings, store = build_agent_service(settings=settings)
    session_id = new_id("session")

    response = service.handle_chat(
        user_context=UserContext(user_id=resolved_settings.default_user_id),
        request=ChatRequest(
            session_id=session_id,
            character_id=resolved_settings.default_character_id,
            message="早",
            metadata={"client": "test"},
        ),
    )

    session = store.get_session(session_id=session_id)
    assert session is not None
    assert session.message_count == 2
    assert response.session_id == session_id
    assert response.character_id == resolved_settings.default_character_id
    assert response.response
    assert response.used_memory_ids == []


def test_agent_service_ended_session_failure_can_be_mapped(settings):
    service, _memory_admin, resolved_settings, store = build_agent_service(settings=settings)
    session = store.create_session(
        user_id=resolved_settings.default_user_id,
        character_id=resolved_settings.default_character_id,
    )
    store.end_session(session_id=session.id, user_id=resolved_settings.default_user_id)

    with pytest.raises(SessionEndedError) as raised:
        service.handle_chat(
            user_context=UserContext(user_id=resolved_settings.default_user_id),
            request=ChatRequest(
                session_id=session.id,
                character_id=resolved_settings.default_character_id,
                message="hello",
            ),
        )

    error_info = map_exception(raised.value)
    assert error_info.code == ErrorCode.SESSION_ENDED
    assert error_info.to_public_dict()["message"] == (
        "Session has ended. Resume it before sending messages."
    )


def test_agent_service_archived_session_failure(settings):
    service, _memory_admin, resolved_settings, store = build_agent_service(settings=settings)
    session = store.create_session(
        user_id=resolved_settings.default_user_id,
        character_id=resolved_settings.default_character_id,
    )
    store.archive_session(session_id=session.id, user_id=resolved_settings.default_user_id)

    with pytest.raises(SessionArchivedError):
        service.handle_chat(
            user_context=UserContext(user_id=resolved_settings.default_user_id),
            request=ChatRequest(
                session_id=session.id,
                character_id=resolved_settings.default_character_id,
                message="hello",
            ),
        )


def test_agent_service_unknown_character_fallback_warning(settings):
    service, _memory_admin, resolved_settings, _store = build_agent_service(settings=settings)

    response = service.handle_chat(
        user_context=UserContext(user_id=resolved_settings.default_user_id),
        request=ChatRequest(
            session_id=new_id("session"),
            character_id="unknown_character",
            message="hello",
        ),
    )

    assert response.character_id == "unknown_character"
    assert "character_id_unknown_using_default_profile" in response.warnings


def test_agent_service_app_log_uses_summary_fields_and_does_not_leak_secrets():
    logger = _RecordingAppLogger()
    service = AgentService(controller=_FailingController(), app_logger=logger)  # type: ignore[arg-type]

    with pytest.raises(ValueError):
        service.handle_chat(
            user_context=UserContext(
                user_id="user_test",
                request_id="request_test",
                metadata={"authorization": "Bearer user-context-secret"},
            ),
            request=ChatRequest(
                session_id="session_test",
                character_id="aemeath",
                message=(
                    "message token=message-secret api-key=message-api-secret "
                    "authorization=Bearer message-auth cookie=message-cookie"
                ),
                metadata={
                    "token": "metadata-token-secret",
                    "api-key": "metadata-api-secret",
                    "authorization": "Bearer metadata-auth-secret",
                    "cookie": "metadata-cookie-secret",
                },
            ),
        )

    assert len(logger.records) == 1
    [record] = logger.records
    assert record.event_name == "agent_service.chat_failed"
    assert record.error_code == ErrorCode.INVALID_INPUT.value
    assert record.payload["status"] == "failed"
    assert "duration_ms" in record.payload

    raw = json.dumps(asdict(record), ensure_ascii=False)
    assert "secret123" not in raw
    assert "secret456" not in raw
    assert "auth-secret" not in raw
    assert "session-secret" not in raw
    assert "user-context-secret" not in raw
    assert "message-secret" not in raw
    assert "message-api-secret" not in raw
    assert "message-auth" not in raw
    assert "message-cookie" not in raw
    assert "metadata-token-secret" not in raw
    assert "metadata-api-secret" not in raw
    assert "metadata-auth-secret" not in raw
    assert "metadata-cookie-secret" not in raw
