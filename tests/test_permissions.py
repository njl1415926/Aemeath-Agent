from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.db.sqlite_store import SQLiteStore
from app.errors import ErrorCode, http_status_for_error, map_exception
from app.memory.admin import MemoryAdminService
from app.permissions.errors import PermissionDeniedError
from app.permissions.service import PermissionService
from app.services.schemas import UserContext
from app.session.errors import SessionOwnershipError


def test_permission_service_owner_only_success_and_failure():
    permissions = PermissionService()
    permissions.require_owner(
        resource_user_id="user_1",
        user_context=UserContext(user_id="user_1"),
    )

    with pytest.raises(PermissionDeniedError) as raised:
        permissions.require_owner(
            resource_user_id="user_1",
            user_context=UserContext(user_id="user_2"),
        )

    error_info = map_exception(raised.value)
    assert error_info.code == ErrorCode.PERMISSION_DENIED
    assert http_status_for_error(error_info) == 403
    public_payload = json.dumps(error_info.to_public_dict())
    log_payload = json.dumps(error_info.to_log_dict())
    assert "user_1" not in public_payload
    assert "user_2" not in public_payload
    assert "user_1" not in log_payload
    assert "user_2" not in log_payload


def test_cross_user_session_operation_is_denied_without_ownership_leak(tmp_path: Path):
    store = SQLiteStore(tmp_path / "permission_sessions.db")
    session = store.create_session(user_id="user_1", character_id="aemeath")

    with pytest.raises(SessionOwnershipError) as raised:
        store.end_session(session_id=session.id, user_id="user_2")

    error_info = map_exception(raised.value)
    assert error_info.code == ErrorCode.SESSION_OWNERSHIP_DENIED
    assert http_status_for_error(error_info) == 403
    assert "user_1" not in json.dumps(error_info.to_public_dict())
    assert "user_2" not in json.dumps(error_info.to_public_dict())


def test_cross_user_memory_management_has_no_side_effect(tmp_path: Path):
    store = SQLiteStore(tmp_path / "permission_memories.db")
    memory_id = store.create_memory(
        user_id="user_1",
        scope="user_global",
        scope_character_id=None,
        scope_session_id=None,
        namespace="user_profile",
        memory_type="preference",
        content="owner-only memory",
        importance=5,
        confidence=0.9,
        source="test",
        source_message_id=None,
        source_session_id=None,
        source_job_id=None,
        source_message_ids=[],
        related_memory_ids=[],
    )
    admin = MemoryAdminService(store, session_store=store)

    assert (
        admin.archive_memory(memory_id=memory_id, user_id="user_2")
        == "memory not found or unavailable"
    )
    assert (
        admin.disable_memory(memory_id=memory_id, user_id="user_2")
        == "memory not found or unavailable"
    )

    memory = store.get_memory(memory_id=memory_id)
    assert memory is not None
    assert memory.status == "active"
    assert memory.is_active == 1
