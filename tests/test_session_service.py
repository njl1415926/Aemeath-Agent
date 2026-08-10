from __future__ import annotations

import pytest

from app.db.sqlite_store import SQLiteStore
from app.errors import AemeathError, ErrorCode
from app.services.schemas import UserContext
from app.services.session_service import SessionService


def test_session_service_rejects_invalid_pagination_limits(tmp_path):
    store = SQLiteStore(tmp_path / "service.db")
    service = SessionService(session_store=store, message_store=store)
    user = UserContext(user_id="user_1")
    session = store.create_session(user_id="user_1", character_id="aemeath")

    for call in (
        lambda: service.list_sessions_page(user_context=user, limit=0),
        lambda: service.list_sessions_page(user_context=user, limit=201),
        lambda: service.list_sessions_page(user_context=user, limit=True),
        lambda: service.list_messages_page(
            user_context=user,
            session_id=session.id,
            character_id="aemeath",
            limit=0,
        ),
        lambda: service.list_messages_page(
            user_context=user,
            session_id=session.id,
            character_id="aemeath",
            limit=501,
        ),
    ):
        with pytest.raises(AemeathError) as exc_info:
            call()
        assert exc_info.value.code == ErrorCode.INVALID_INPUT
        assert exc_info.value.public_message == "Invalid pagination limit."
