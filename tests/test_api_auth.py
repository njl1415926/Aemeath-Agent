from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.api.server import create_app
from app.auth.tokens import new_token
from app.errors import ErrorCode

ORIGIN = "https://testserver"
PROVENANCE = {"Origin": ORIGIN, "Sec-Fetch-Site": "same-origin"}


def _web_client(settings):
    web_settings = replace(
        settings,
        api_auth_mode="web_session",
        api_allow_local_default_user=False,
        api_cors_allowed_origins=(),
        web_public_origin=ORIGIN,
        web_auth_csrf_key=b"csrf-test-key-material-exactly-32!",
    )
    app = create_app(web_settings)
    return TestClient(app, base_url=ORIGIN), app


def _accept_invitation(client: TestClient, app, *, username: str = "alice"):
    issued = app.state.runtime.browser_auth.invitations.issue(
        username=username,
        display_name="Alice",
        roles=["user"],
    )
    response = client.post(
        "/v1/auth/invitations/accept",
        headers={**PROVENANCE, "X-Request-ID": "req_invite_accept"},
        json={"token": issued.raw_token, "password": "correct horse battery staple"},
    )
    assert response.status_code == 201
    return response


def _csrf(client: TestClient) -> str:
    response = client.get("/v1/auth/csrf")
    assert response.status_code == 200
    return str(response.json()["csrf_token"])


def test_all_auth_endpoints_cookie_rotation_no_store_and_general_csrf(settings):
    client, app = _web_client(settings)
    accepted = _accept_invitation(client, app)
    first_cookie = client.cookies.get("__Host-aemeath_session")
    assert first_cookie
    set_cookie = accepted.headers["set-cookie"].lower()
    assert "secure" in set_cookie
    assert "httponly" in set_cookie
    assert "samesite=strict" in set_cookie
    assert "path=/" in set_cookie
    assert "domain=" not in set_cookie
    assert "expires=" not in set_cookie
    assert accepted.headers["Cache-Control"] == "no-store"
    assert accepted.headers["X-Request-ID"] == "req_invite_accept"

    me = client.get("/v1/auth/me")
    assert me.status_code == 200
    assert me.json()["username"] == "alice"
    assert me.headers["Cache-Control"] == "no-store"

    csrf = _csrf(client)
    missing_csrf = client.post(
        "/v1/sessions",
        headers=PROVENANCE,
        json={"character_id": "aemeath"},
    )
    assert missing_csrf.status_code == 403
    assert missing_csrf.json()["error"]["code"] == ErrorCode.CSRF_INVALID.value
    created = client.post(
        "/v1/sessions",
        headers={**PROVENANCE, "X-CSRF-Token": csrf},
        json={"character_id": "aemeath"},
    )
    assert created.status_code == 201
    assert created.json()["user_id"] == accepted.json()["account_id"]

    changed = client.post(
        "/v1/auth/password/change",
        headers={**PROVENANCE, "X-CSRF-Token": csrf},
        json={
            "current_password": "correct horse battery staple",
            "new_password": "new correct horse battery staple",
        },
    )
    assert changed.status_code == 204
    second_cookie = client.cookies.get("__Host-aemeath_session")
    assert second_cookie and second_cookie != first_cookie
    assert changed.headers["Cache-Control"] == "no-store"

    csrf = _csrf(client)
    logged_out = client.post(
        "/v1/auth/logout",
        headers={
            **PROVENANCE,
            "Content-Type": "application/json",
            "X-CSRF-Token": csrf,
        },
        content=b"",
    )
    assert logged_out.status_code == 204
    assert "max-age=0" in logged_out.headers["set-cookie"].lower()

    logged_in = client.post(
        "/v1/auth/login",
        headers=PROVENANCE,
        json={"username": "alice", "password": "new correct horse battery staple"},
    )
    assert logged_in.status_code == 200
    login_cookie = client.cookies.get("__Host-aemeath_session")
    assert login_cookie

    reset = app.state.runtime.browser_auth.password_resets.issue(account_selector="alice")
    reset_accepted = client.post(
        "/v1/auth/password-reset/accept",
        headers=PROVENANCE,
        json={"token": reset.raw_token, "new_password": "reset browser password material"},
    )
    assert reset_accepted.status_code == 200
    assert client.cookies.get("__Host-aemeath_session") != login_cookie
    assert reset_accepted.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize(
    ("headers", "expected_code"),
    [
        ({"Origin": "https://evil.test"}, ErrorCode.CSRF_INVALID),
        ({"Origin": "null"}, ErrorCode.CSRF_INVALID),
        ({"Referer": "https://testserver.evil.test/path"}, ErrorCode.CSRF_INVALID),
        ({"Origin": ORIGIN, "Sec-Fetch-Site": "same-site"}, ErrorCode.CSRF_INVALID),
        ({"Origin": ORIGIN, "Content-Type": "text/plain"}, ErrorCode.CONTENT_TYPE_UNSUPPORTED),
    ],
)
def test_unsafe_web_request_provenance_matrix(settings, headers, expected_code):
    client, _app = _web_client(settings)
    response = client.post(
        "/v1/auth/login",
        headers={"Content-Type": "application/json", **headers},
        content=b'{"username":"alice","password":"unique-secret-marker"}',
    )
    assert response.status_code in {403, 415}
    assert response.json()["error"]["code"] == expected_code.value
    assert "unique-secret-marker" not in response.text


def test_referer_fallback_and_generic_invitation_reset_errors_skip_argon(settings):
    client, app = _web_client(settings)
    browser = app.state.runtime.browser_auth
    password_engine = browser.sessions.passwords
    original_hash = password_engine.hash_password
    calls = []

    def counted_hash(password: str) -> str:
        calls.append(password)
        return original_hash(password)

    password_engine.hash_password = counted_hash
    invite = client.post(
        "/v1/auth/invitations/accept",
        headers={"Referer": f"{ORIGIN}/invite", "Sec-Fetch-Site": "same-origin"},
        json={
            "token": new_token("invitation"),
            "password": "invite unique secret marker",
        },
    )
    reset = client.post(
        "/v1/auth/password-reset/accept",
        headers=PROVENANCE,
        json={
            "token": new_token("password_reset"),
            "new_password": "reset unique secret marker",
        },
    )
    assert invite.status_code == 400
    assert invite.json()["error"]["code"] == ErrorCode.INVITATION_INVALID_OR_EXPIRED.value
    assert reset.status_code == 400
    assert reset.json()["error"]["code"] == ErrorCode.PASSWORD_RESET_INVALID_OR_EXPIRED.value
    assert calls == []
    assert "unique secret marker" not in invite.text + reset.text


def test_invalid_session_is_cleared_and_auth_modes_reject_wrong_credential(settings):
    client, _app = _web_client(settings)
    missing = client.get("/v1/auth/me")
    assert missing.status_code == 401
    assert missing.json()["error"]["code"] == ErrorCode.AUTH_REQUIRED.value
    assert "set-cookie" not in missing.headers

    client.cookies.set("__Host-aemeath_session", new_token("web_session"))
    invalid = client.get("/v1/auth/me")
    assert invalid.status_code == 401
    assert invalid.json()["error"]["code"] == ErrorCode.AUTH_SESSION_REVOKED.value
    assert "max-age=0" in invalid.headers["set-cookie"].lower()

    bearer_in_web = client.get("/v1/sessions", headers={"Authorization": "Bearer must-not-leak"})
    assert bearer_in_web.status_code == 400
    assert bearer_in_web.json()["error"]["code"] == ErrorCode.AUTH_CREDENTIAL_CONFLICT.value
    assert "must-not-leak" not in bearer_in_web.text

    dev_app = create_app(replace(settings, dev_auth_token="dev-token"))
    dev_client = TestClient(dev_app, base_url=ORIGIN)
    dev_client.cookies.set("__Host-aemeath_session", new_token("web_session"))
    cookie_in_dev = dev_client.get("/v1/sessions")
    assert cookie_in_dev.status_code == 400
    assert cookie_in_dev.json()["error"]["code"] == ErrorCode.AUTH_CREDENTIAL_CONFLICT.value


def test_duplicate_cookie_and_bearer_cookie_conflict_are_rejected(settings):
    client, _app = _web_client(settings)
    token = new_token("web_session")
    duplicate = client.get(
        "/v1/auth/me",
        headers={"Cookie": f"__Host-aemeath_session={token}; __Host-aemeath_session={token}"},
    )
    assert duplicate.status_code == 400
    assert duplicate.json()["error"]["code"] == ErrorCode.AUTH_CREDENTIAL_CONFLICT.value
    assert "max-age=0" in duplicate.headers["set-cookie"].lower()

    conflict = client.get(
        "/v1/auth/me",
        headers={
            "Cookie": f"__Host-aemeath_session={token}",
            "Authorization": "Bearer conflict-secret",
        },
    )
    assert conflict.status_code == 400
    assert "conflict-secret" not in conflict.text


@pytest.mark.parametrize(
    ("content", "json_body"),
    [
        (b'{"username":', None),
        (None, {"username": "alice"}),
    ],
    ids=["malformed-json", "dto-validation"],
)
def test_invalid_login_bodies_consume_early_admission_and_skip_password_work(
    settings,
    content,
    json_body,
):
    client, app = _web_client(
        replace(
            settings,
            auth_pre_rate_per_minute=1,
            auth_pre_burst=1,
            auth_pre_global_rate_per_minute=100,
            auth_pre_global_burst=100,
        )
    )
    password_engine = app.state.runtime.browser_auth.sessions.passwords
    password_calls: list[str] = []

    def unexpected_password_work(*_args, **_kwargs):
        password_calls.append("called")
        raise AssertionError("password work must not run")

    password_engine.hash_password = unexpected_password_work
    password_engine.verify_password = unexpected_password_work
    password_engine.verify_dummy = unexpected_password_work

    kwargs = {"headers": {**PROVENANCE, "Content-Type": "application/json"}}
    if content is not None:
        kwargs["content"] = content
    else:
        kwargs["json"] = json_body
    invalid = client.post("/v1/auth/login", **kwargs)
    assert invalid.status_code == 400
    assert invalid.json()["error"]["code"] == ErrorCode.INVALID_INPUT.value

    blocked = client.post(
        "/v1/auth/login",
        headers=PROVENANCE,
        json={"username": "alice", "password": "valid password material"},
    )
    assert blocked.status_code == 429
    assert "Retry-After" in blocked.headers
    assert password_calls == []


def test_login_identity_stage_does_not_double_consume_request_admission(settings):
    client, app = _web_client(
        replace(
            settings,
            auth_pre_rate_per_minute=1,
            auth_pre_burst=2,
            auth_pre_global_rate_per_minute=100,
            auth_pre_global_burst=100,
        )
    )
    password_engine = app.state.runtime.browser_auth.sessions.passwords
    calls: list[str] = []

    def counted_dummy(password: str) -> None:
        calls.append(password)

    password_engine.verify_dummy = counted_dummy
    body = {"username": "missing_user", "password": "valid password material"}
    assert client.post("/v1/auth/login", headers=PROVENANCE, json=body).status_code == 401
    assert client.post("/v1/auth/login", headers=PROVENANCE, json=body).status_code == 401
    assert client.post("/v1/auth/login", headers=PROVENANCE, json=body).status_code == 429
    assert calls == ["valid password material", "valid password material"]
