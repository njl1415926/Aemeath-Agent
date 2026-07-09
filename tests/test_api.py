from __future__ import annotations

import json
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError

from fastapi.testclient import TestClient

from app.accounts.service import AccountService
from app.api.server import create_app
from app.auth.service import hash_dev_token
from app.errors import ErrorCode
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
