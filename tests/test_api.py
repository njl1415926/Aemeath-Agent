from __future__ import annotations

import json
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from threading import Event, Thread
from time import sleep
from urllib.error import HTTPError

import pytest
from fastapi.testclient import TestClient

from app.accounts.service import AccountService
from app.api.server import create_app
from app.auth.service import hash_dev_token
from app.db.stream_turns import StreamTurnComplete, StreamTurnResolveOrReserve
from app.errors import ErrorCode
from app.runtime.guardrails.schemas import DialogueAdmissionRequest
from app.schemas import StateUpdate
from app.services.schemas import UserContext
from app.services.stream_fingerprint import fingerprint_chat_request_v1
from app.services.stream_schemas import StreamChatRequest
from app.session.errors import SessionOwnershipError
from app.utils.ids import new_id


def _client(settings, *, token: str = "correct-dev-token") -> tuple[TestClient, object]:
    api_settings = replace(settings, dev_auth_token=token)
    app = create_app(api_settings)
    return TestClient(app), app


def _auth(token: str = "correct-dev-token") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_api_health_does_not_require_auth(settings):
    app = create_app(settings)
    client = TestClient(app)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "aemeath-agent"}
    assert response.headers["X-Request-ID"].startswith("req_")


def test_api_chat_success_uses_agent_service(settings):
    client, app = _client(settings)
    session_id = new_id("session")

    response = client.post(
        "/v1/chat",
        headers={**_auth(), "X-Request-ID": "req_test_chat"},
        json={
            "session_id": session_id,
            "character_id": app.state.runtime.settings.default_character_id,
            "message": "hello",
            "metadata": {"client": "test"},
        },
    )

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "req_test_chat"
    payload = response.json()
    assert payload["session_id"] == session_id
    assert payload["turn_id"].startswith("turn_")
    assert payload["response"]

    session = app.state.runtime.store.get_session(session_id=session_id)
    assert session is not None
    assert session.message_count == 2


def test_api_missing_token_is_rejected_by_default(settings):
    client, _app = _client(settings)

    response = client.post(
        "/v1/chat",
        json={
            "session_id": new_id("session"),
            "character_id": "aemeath",
            "message": "hello",
        },
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == ErrorCode.AUTH_REQUIRED.value


def test_api_local_default_user_requires_explicit_opt_in(settings):
    api_settings = replace(settings, api_allow_local_default_user=True)
    app = create_app(api_settings)
    client = TestClient(app)

    response = client.post(
        "/v1/chat",
        json={
            "session_id": new_id("session"),
            "character_id": "aemeath",
            "message": "hello",
        },
    )

    assert response.status_code == 200


def test_api_invalid_token_maps_to_auth_error(settings):
    client, _app = _client(settings)

    response = client.get("/v1/sessions", headers=_auth("wrong-token-secret"))

    assert response.status_code == 401
    payload = response.json()
    assert payload["error"]["code"] == ErrorCode.AUTH_INVALID_CREDENTIALS.value
    assert "wrong-token-secret" not in response.text


def test_api_disabled_account_maps_to_auth_error(settings):
    client, app = _client(
        replace(settings, dev_auth_user_id="disabled_user"),
        token="disabled-token",
    )
    app.state.runtime.store.set_account_status(
        account_id="disabled_user",
        status="disabled",
    )

    response = client.get("/v1/sessions", headers=_auth("disabled-token"))

    assert response.status_code == 403
    assert response.json()["error"]["code"] == ErrorCode.AUTH_ACCOUNT_DISABLED.value
    assert "disabled-token" not in response.text


def test_api_chat_ended_and_archived_session_errors(settings):
    client, app = _client(settings)
    store = app.state.runtime.store
    user_id = app.state.runtime.settings.default_user_id
    character_id = app.state.runtime.settings.default_character_id
    ended = store.create_session(user_id=user_id, character_id=character_id)
    archived = store.create_session(user_id=user_id, character_id=character_id)
    store.end_session(session_id=ended.id, user_id=user_id)
    store.archive_session(session_id=archived.id, user_id=user_id)

    ended_response = client.post(
        "/v1/chat",
        headers=_auth(),
        json={
            "session_id": ended.id,
            "character_id": character_id,
            "message": "hello",
        },
    )
    archived_response = client.post(
        "/v1/chat",
        headers=_auth(),
        json={
            "session_id": archived.id,
            "character_id": character_id,
            "message": "hello",
        },
    )

    assert ended_response.status_code == 409
    assert ended_response.json()["error"]["code"] == ErrorCode.SESSION_ENDED.value
    assert archived_response.status_code == 409
    assert archived_response.json()["error"]["code"] == ErrorCode.SESSION_ARCHIVED.value


def test_api_runtime_session_lifecycle_uses_shared_dialogue_admission(settings):
    client, app = _client(settings)
    del client
    runtime = app.state.runtime

    assert runtime.session_service.admission is runtime.agent_service.admission
    assert runtime.agent_stream_service.admission is runtime.agent_service.admission


def test_session_lifecycle_waits_for_active_stream_admission(settings):
    client, app = _client(
        replace(
            settings,
            runtime_session_wait_timeout_seconds=1.0,
            runtime_dialogue_max_concurrency=2,
        )
    )
    del client
    runtime = app.state.runtime
    user_id = runtime.settings.default_user_id
    character_id = runtime.settings.default_character_id
    session = runtime.store.create_session(user_id=user_id, character_id=character_id)
    user_context = UserContext(user_id=user_id, request_id="req_lifecycle_wait")
    completed = Event()
    result: list[str] = []
    errors: list[BaseException] = []

    def archive_session() -> None:
        try:
            archived = runtime.session_service.archive_session(
                user_context=user_context,
                session_id=session.id,
                character_id=character_id,
            )
            result.append(archived.status)
        except BaseException as exc:
            errors.append(exc)
        finally:
            completed.set()

    handle = runtime.agent_stream_service.admission.open_handle(
        request=DialogueAdmissionRequest(
            user_id=user_id,
            session_id=session.id,
            request_id="req_stream_holds_admission",
        )
    )
    try:
        worker = Thread(target=archive_session)
        worker.start()
        sleep(0.05)
        assert not completed.is_set()
        assert runtime.store.get_session(session_id=session.id).status == "active"
    finally:
        handle.close()

    worker.join(timeout=2)
    assert completed.is_set()
    assert errors == []
    assert result == ["archived"]
    assert runtime.store.get_session(session_id=session.id).status == "archived"
    assert runtime.agent_service.admission.entry_count == 0


def test_session_lifecycle_waits_for_terminal_replay_handle(settings):
    client, app = _client(
        replace(
            settings,
            runtime_session_wait_timeout_seconds=1.0,
            runtime_dialogue_max_concurrency=2,
        )
    )
    del client
    runtime = app.state.runtime
    user_id = runtime.settings.default_user_id
    character_id = runtime.settings.default_character_id
    session = runtime.store.create_session(user_id=user_id, character_id=character_id)
    request = StreamChatRequest(
        session_id=session.id,
        character_id=character_id,
        message="hello replay",
        client_message_id="cm-session-lifecycle-replay",
        metadata={},
    )
    fingerprint = fingerprint_chat_request_v1(
        character_id=request.character_id,
        message=request.message,
        metadata=request.metadata,
    )
    reserved = runtime.store.resolve_or_reserve(
        request=StreamTurnResolveOrReserve(
            user_id=user_id,
            session_id=session.id,
            active_character_id=character_id,
            client_message_id=request.client_message_id,
            request_fingerprint_version=1,
            request_fingerprint=fingerprint,
            owner_runtime_id=runtime.agent_stream_service.owner_runtime_id,
            turn_id=new_id("turn"),
            user_message_id=new_id("message"),
            user_message_content=request.message,
        )
    )
    completed_record = runtime.store.complete(
        request=StreamTurnComplete(
            record_id=reserved.record.id,
            turn_id=reserved.record.turn_id,
            user_id=user_id,
            session_id=session.id,
            owner_runtime_id=runtime.agent_stream_service.owner_runtime_id,
            assistant_response="durable replay response",
            state_update=StateUpdate(
                user_emotion="neutral",
                aemeath_mood="calm",
                trust_delta=0,
                affection_delta=0,
                familiarity_delta=0,
                last_topic=None,
                reason="test",
            ),
            warnings=(),
        )
    )
    replay = runtime.agent_stream_service.stream_chat(
        user_context=UserContext(user_id=user_id, request_id="req_replay"),
        request=request,
    )
    completed = Event()
    result: list[str] = []
    errors: list[BaseException] = []

    def archive_session() -> None:
        try:
            archived = runtime.session_service.archive_session(
                user_context=UserContext(user_id=user_id, request_id="req_archive"),
                session_id=session.id,
                character_id=character_id,
            )
            result.append(archived.status)
        except BaseException as exc:
            errors.append(exc)
        finally:
            completed.set()

    worker = Thread(target=archive_session)
    worker.start()
    sleep(0.05)
    assert not completed.is_set()
    assert runtime.store.get_session(session_id=session.id).status == "active"

    replay.close()
    worker.join(timeout=2)

    assert completed.is_set()
    assert errors == []
    assert result == ["archived"]
    assert completed_record.status == "completed"
    assert runtime.store.get_session(session_id=session.id).status == "archived"
    assert runtime.agent_service.admission.entry_count == 0


def test_session_lifecycle_failure_releases_admission_and_keeps_stream_durable_result(
    settings,
):
    client, app = _client(settings)
    del client
    runtime = app.state.runtime
    user_id = runtime.settings.default_user_id
    character_id = runtime.settings.default_character_id
    session = runtime.store.create_session(user_id=user_id, character_id=character_id)
    model = runtime.agent_stream_service.model_client.delegate
    request = StreamChatRequest(
        session_id=session.id,
        character_id=character_id,
        message="durable failure boundary",
        client_message_id="cm-session-lifecycle-failure",
        metadata={},
    )
    fingerprint = fingerprint_chat_request_v1(
        character_id=request.character_id,
        message=request.message,
        metadata=request.metadata,
    )
    reserved = runtime.store.resolve_or_reserve(
        request=StreamTurnResolveOrReserve(
            user_id=user_id,
            session_id=session.id,
            active_character_id=character_id,
            client_message_id=request.client_message_id,
            request_fingerprint_version=1,
            request_fingerprint=fingerprint,
            owner_runtime_id=runtime.agent_stream_service.owner_runtime_id,
            turn_id=new_id("turn"),
            user_message_id=new_id("message"),
            user_message_content=request.message,
        )
    )
    completed_record = runtime.store.complete(
        request=StreamTurnComplete(
            record_id=reserved.record.id,
            turn_id=reserved.record.turn_id,
            user_id=user_id,
            session_id=session.id,
            owner_runtime_id=runtime.agent_stream_service.owner_runtime_id,
            assistant_response="durable still here",
            state_update=StateUpdate(
                user_emotion="neutral",
                aemeath_mood="calm",
                trust_delta=0,
                affection_delta=0,
                familiarity_delta=0,
                last_topic=None,
                reason="test",
            ),
            warnings=(),
        )
    )
    runtime.store.save_message(
        session_id=session.id,
        user_id=user_id,
        role="user",
        content="existing user message",
    )
    user_context = UserContext(user_id=user_id, request_id="req_lifecycle_failure")

    with pytest.raises(SessionOwnershipError):
        runtime.session_service.end_session(
            user_context=user_context,
            session_id=session.id,
            character_id="wrong-character",
        )

    with runtime.agent_service.admission.acquire(
        request=DialogueAdmissionRequest(user_id=user_id, session_id=session.id)
    ):
        pass
    assert runtime.agent_service.admission.entry_count == 0
    assert runtime.store.get_session(session_id=session.id).status == "active"
    assert model.open_count == 0
    replayed = runtime.store.resolve_or_reserve(
        request=StreamTurnResolveOrReserve(
            user_id=user_id,
            session_id=session.id,
            active_character_id=character_id,
            client_message_id=request.client_message_id,
            request_fingerprint_version=1,
            request_fingerprint=fingerprint,
            owner_runtime_id=runtime.agent_stream_service.owner_runtime_id,
            turn_id=new_id("turn"),
            user_message_id=new_id("message"),
            user_message_content=request.message,
        )
    )
    assert replayed.kind == "replay_completed"
    assert replayed.record.id == completed_record.id
    assert replayed.assistant_message is not None
    assert replayed.assistant_message.content == "durable still here"
    messages = runtime.store.list_messages_by_session(session_id=session.id)
    assert [message.content for message in messages] == [
        "durable failure boundary",
        "durable still here",
        "existing user message",
    ]


def test_api_sessions_create_list_and_messages(settings):
    client, app = _client(settings)
    character_id = app.state.runtime.settings.default_character_id

    create_response = client.post(
        "/v1/sessions",
        headers=_auth(),
        json={"character_id": character_id, "title": "test session"},
    )
    assert create_response.status_code == 201
    session_id = create_response.json()["id"]

    list_response = client.get("/v1/sessions", headers=_auth())
    assert list_response.status_code == 200
    assert session_id in [item["id"] for item in list_response.json()["sessions"]]

    chat_response = client.post(
        "/v1/chat",
        headers=_auth(),
        json={
            "session_id": session_id,
            "character_id": character_id,
            "message": "hello",
        },
    )
    assert chat_response.status_code == 200

    messages_response = client.get(
        f"/v1/sessions/{session_id}/messages",
        headers=_auth(),
    )
    assert messages_response.status_code == 200
    messages = messages_response.json()["messages"]
    assert [message["role"] for message in messages] == ["user", "assistant"]


def test_api_session_pagination_title_and_lifecycle(settings):
    client, app = _client(settings)
    character_id = app.state.runtime.settings.default_character_id

    created_ids: list[str] = []
    for index in range(3):
        response = client.post(
            "/v1/sessions",
            headers=_auth(),
            json={"character_id": character_id, "title": f"session {index}"},
        )
        assert response.status_code == 201
        created_ids.append(response.json()["id"])

    first_page = client.get("/v1/sessions?limit=1", headers=_auth())
    assert first_page.status_code == 200
    first_payload = first_page.json()
    assert "sessions" in first_payload
    assert "items" not in first_payload
    assert first_payload["has_more"] is True
    assert len(first_payload["sessions"]) == 1

    second_page = client.get(
        "/v1/sessions",
        headers=_auth(),
        params={"limit": 1, "cursor": first_payload["next_cursor"]},
    )
    assert second_page.status_code == 200
    assert len(second_page.json()["sessions"]) == 1

    session_id = created_ids[0]
    update = client.patch(
        f"/v1/sessions/{session_id}",
        headers=_auth(),
        json={"character_id": character_id, "title": "  cafe\u0301\t night  "},
    )
    assert update.status_code == 200
    assert update.json()["title"] == "café night"

    zero_width = client.patch(
        f"/v1/sessions/{session_id}",
        headers=_auth(),
        json={"character_id": character_id, "title": "A\u200dB"},
    )
    assert zero_width.status_code == 200
    assert zero_width.json()["title"] == "A\u200dB"

    missing_title = client.patch(
        f"/v1/sessions/{session_id}",
        headers=_auth(),
        json={"character_id": character_id},
    )
    assert missing_title.status_code == 400
    assert missing_title.json()["error"]["code"] == ErrorCode.INVALID_INPUT.value
    assert app.state.runtime.store.get_session(session_id=session_id).title == "A\u200dB"

    ended = client.post(
        f"/v1/sessions/{session_id}/end",
        headers=_auth(),
        json={"character_id": character_id},
    )
    assert ended.status_code == 200
    assert ended.json()["status"] == "ended"

    resumed = client.post(
        f"/v1/sessions/{session_id}/resume",
        headers=_auth(),
        json={"character_id": character_id},
    )
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "active"

    archived = client.post(
        f"/v1/sessions/{session_id}/archive",
        headers=_auth(),
        json={"character_id": character_id},
    )
    assert archived.status_code == 200
    assert archived.json()["status"] == "archived"

    update_archived = client.patch(
        f"/v1/sessions/{session_id}",
        headers=_auth(),
        json={"character_id": character_id, "title": "blocked"},
    )
    assert update_archived.status_code == 409
    assert update_archived.json()["error"]["code"] == ErrorCode.SESSION_ARCHIVED.value


def test_api_invalid_cursor_and_archived_history_contract(settings):
    client, app = _client(settings)
    character_id = app.state.runtime.settings.default_character_id
    user_id = app.state.runtime.settings.default_user_id
    store = app.state.runtime.store
    session = store.create_session(user_id=user_id, character_id=character_id)
    store.save_message(
        session_id=session.id,
        user_id=user_id,
        role="user",
        content="archived hello",
    )
    store.archive_session(session_id=session.id, user_id=user_id)

    invalid = client.get("/v1/sessions", headers=_auth(), params={"cursor": "not-json"})
    assert invalid.status_code == 400
    assert invalid.json()["error"] == {
        "code": ErrorCode.INVALID_INPUT.value,
        "message": "Invalid cursor.",
        "retryable": False,
    }
    assert "not-json" not in invalid.text

    history = client.get(
        f"/v1/sessions/{session.id}/messages",
        headers=_auth(),
        params={"character_id": character_id},
    )
    assert history.status_code == 200
    payload = history.json()
    assert "messages" in payload
    assert "items" not in payload
    assert payload["messages"][0]["content"] == "archived hello"


def test_api_session_messages_reject_cross_user_access(settings):
    client, app = _client(settings)
    store = app.state.runtime.store
    character_id = app.state.runtime.settings.default_character_id
    user_id = app.state.runtime.settings.default_user_id
    session = store.create_session(user_id=user_id, character_id=character_id)
    account_service = AccountService(store)
    account_service.set_dev_token_hash(
        account_id="other_user",
        dev_token_hash=hash_dev_token("other-token"),
    )

    response = client.get(
        f"/v1/sessions/{session.id}/messages",
        headers=_auth("other-token"),
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == ErrorCode.SESSION_OWNERSHIP_DENIED.value


def test_api_validation_error_uses_stable_error_shape(settings):
    client, _app = _client(settings)

    response = client.post(
        "/v1/chat",
        headers=_auth(),
        json={"session_id": new_id("session"), "character_id": "aemeath"},
    )

    assert response.status_code == 400
    payload = response.json()
    assert payload["error"] == {
        "code": ErrorCode.INVALID_INPUT.value,
        "message": "Invalid input.",
        "retryable": False,
    }
    assert "detail" not in payload


def test_api_does_not_leak_sensitive_values_in_response_or_app_log(settings):
    api_settings = replace(
        settings,
        enable_app_log=True,
        app_log_root=settings.db_path.parent / ".aemeath" / "app_logs",
        dev_auth_token="correct-dev-token",
    )
    app = create_app(api_settings)
    client = TestClient(app)

    response = client.get(
        "/v1/sessions",
        headers={
            "Authorization": "Bearer wrong-token-secret",
            "Cookie": "session=real-cookie-secret",
        },
    )

    assert response.status_code == 401
    assert "wrong-token-secret" not in response.text
    assert "real-cookie-secret" not in response.text

    [log_file] = list(api_settings.app_log_root.glob("*/*/*/*.jsonl"))
    raw_log = log_file.read_text(encoding="utf-8")
    assert "wrong-token-secret" not in raw_log
    assert "real-cookie-secret" not in raw_log


def test_api_session_app_logs_safe_success_and_failure_summaries(settings):
    secret_title = "secret title marker"
    raw_cursor = "bad-cursor!"
    api_settings = replace(
        settings,
        enable_app_log=True,
        app_log_root=settings.db_path.parent / ".aemeath" / "app_logs",
        dev_auth_token="correct-dev-token",
    )
    app = create_app(api_settings)
    client = TestClient(app)
    character_id = app.state.runtime.settings.default_character_id

    created = client.post(
        "/v1/sessions",
        headers=_auth(),
        json={"character_id": character_id, "title": secret_title},
    )
    assert created.status_code == 201
    session_id = created.json()["id"]
    patched = client.patch(
        f"/v1/sessions/{session_id}",
        headers=_auth(),
        json={"character_id": character_id, "title": secret_title + " updated"},
    )
    assert patched.status_code == 200
    failed = client.get("/v1/sessions", headers=_auth(), params={"cursor": raw_cursor})
    assert failed.status_code == 400

    [log_file] = list(api_settings.app_log_root.glob("*/*/*/*.jsonl"))
    raw_log = log_file.read_text(encoding="utf-8")
    assert secret_title not in raw_log
    assert raw_cursor not in raw_log
    records = [
        json.loads(line)
        for line in raw_log.splitlines()
        if line.strip()
    ]
    by_name = {record["event_name"]: record for record in records}
    assert by_name["api.session.created"]["payload"]["title_length"] == len(secret_title)
    assert "duration_ms" in by_name["api.session.created"]["payload"]
    assert by_name["api.session.title_updated"]["payload"]["title_action"] == "set"
    assert by_name["api.session.failed"]["error_code"] == ErrorCode.INVALID_INPUT.value
    assert (
        by_name["api.session.failed"]["payload"]["cursor_error_reason"]
        == "invalid_base64url_charset"
    )


def test_api_prompt_run_log_is_disabled_by_default_even_when_cli_default_is_enabled(settings):
    api_settings = replace(
        settings,
        dev_auth_token="correct-dev-token",
        enable_prompt_run_log=True,
        api_enable_prompt_run_log=False,
    )
    app = create_app(api_settings)
    client = TestClient(app)

    response = client.post(
        "/v1/chat",
        headers=_auth(),
        json={
            "session_id": new_id("session"),
            "character_id": "aemeath",
            "message": "hello",
        },
    )

    assert response.status_code == 200
    assert list(api_settings.prompt_run_log_root.glob("*/*/*/*.jsonl")) == []


def test_api_prompt_run_log_requires_explicit_api_opt_in(settings):
    api_settings = replace(
        settings,
        dev_auth_token="correct-dev-token",
        enable_prompt_run_log=False,
        api_enable_prompt_run_log=True,
    )
    app = create_app(api_settings)
    client = TestClient(app)

    response = client.post(
        "/v1/chat",
        headers=_auth(),
        json={
            "session_id": new_id("session"),
            "character_id": "aemeath",
            "message": "hello",
        },
    )

    assert response.status_code == 200
    [log_file] = list(api_settings.prompt_run_log_root.glob("*/*/*/*.jsonl"))
    assert response.json()["turn_id"] in log_file.read_text(encoding="utf-8")


def test_api_request_id_flows_to_trace_metadata(settings):
    api_settings = replace(
        settings,
        dev_auth_token="correct-dev-token",
        enable_trace_log=True,
    )
    app = create_app(api_settings)
    client = TestClient(app)

    response = client.post(
        "/v1/chat",
        headers={**_auth(), "X-Request-ID": "req_trace_api"},
        json={
            "session_id": new_id("session"),
            "character_id": "aemeath",
            "message": "hello",
        },
    )

    assert response.status_code == 200
    [trace_file] = list(api_settings.trace_log_root.glob("*/*/*/*.jsonl"))
    records = [
        json.loads(line)
        for line in trace_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert any(
        record["event_type"] == "turn_started"
        and record["payload"]["metadata"]["request_id"] == "req_trace_api"
        for record in records
    )


def test_api_model_provider_error_does_not_fallback(settings, monkeypatch):
    class FailingDeepSeekClient:
        def __init__(self, **_kwargs):
            pass

        def chat(self, *, messages):
            raise HTTPError(
                url="https://api.deepseek.com/chat/completions",
                code=401,
                msg="provider auth failed token=secret-provider-token",
                hdrs={},
                fp=BytesIO(b"api_key=secret-provider-key"),
            )

        def close(self):
            return None

    monkeypatch.setattr("app.main.DeepSeekClient", FailingDeepSeekClient)
    api_settings = replace(
        settings,
        use_mock_llm=False,
        deepseek_api_key="test-api-key",
        dev_auth_token="correct-dev-token",
        enable_model_fallback=True,
        api_enable_model_fallback=False,
    )
    app = create_app(api_settings)
    client = TestClient(app)

    response = client.post(
        "/v1/chat",
        headers=_auth(),
        json={
            "session_id": new_id("session"),
            "character_id": "aemeath",
            "message": "hello",
        },
    )

    assert response.status_code == 502
    payload = response.json()
    assert payload["error"]["code"] == ErrorCode.MODEL_AUTH_FAILED.value
    assert "secret-provider-token" not in response.text
    assert "secret-provider-key" not in response.text


def test_api_does_not_couple_dialogue_controller_to_http():
    controller_source = Path("app/agent/dialogue_controller.py").read_text(
        encoding="utf-8"
    )
    chat_route_source = Path("app/api/routes/chat.py").read_text(encoding="utf-8")

    assert "fastapi" not in controller_source.lower()
    assert "HTTPException" not in controller_source
    assert "DialogueController" not in chat_route_source
