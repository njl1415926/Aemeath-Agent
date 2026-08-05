from __future__ import annotations

import ast
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.auth.browser_sessions import BrowserSessionAuthService
from app.auth.errors import (
    AuthInvalidCredentialsError,
    AuthSessionRevokedError,
    InvitationInvalidError,
    PasswordResetInvalidError,
)
from app.auth.invitations import InvitationService, PasswordResetService
from app.auth.passwords import validate_password
from app.auth.tokens import new_token
from app.db.sqlite_store import SQLiteStore
from app.observability.schemas import AppLogRecord


@dataclass
class RecordingLogger:
    records: list[AppLogRecord] = field(default_factory=list)

    def record(self, record: AppLogRecord) -> None:
        self.records.append(record)


class FailingLogger:
    def record(self, record: AppLogRecord) -> None:
        del record
        raise RuntimeError("logger unavailable")


class FastPasswords:
    def __init__(self):
        self.hash_calls = 0
        self.verify_calls = 0
        self.dummy_calls = 0

    def hash_password(self, password: str) -> str:
        self.hash_calls += 1
        return "fast:" + validate_password(password)

    def verify_password(self, encoded_hash: str, password: str) -> bool:
        self.verify_calls += 1
        return encoded_hash == "fast:" + password

    def verify_dummy(self, password: str) -> None:
        del password
        self.dummy_calls += 1

    def check_needs_rehash(self, encoded_hash: str) -> bool:
        del encoded_hash
        return False


def _services(tmp_path: Path, *, logger=None, clock=None):
    store = SQLiteStore(tmp_path / "browser-auth.db")
    passwords = FastPasswords()
    app_logger = logger or RecordingLogger()
    sessions = BrowserSessionAuthService(
        store=store.browser_auth,
        passwords=passwords,  # type: ignore[arg-type]
        app_logger=app_logger,
        idle_seconds=600,
        absolute_seconds=3600,
        touch_seconds=60,
        clock=clock,
    )
    invitations = InvitationService(
        store=store.browser_auth,
        passwords=passwords,  # type: ignore[arg-type]
        sessions=sessions,
        app_logger=app_logger,
        public_origin="https://example.test",
    )
    resets = PasswordResetService(
        store=store.browser_auth,
        passwords=passwords,  # type: ignore[arg-type]
        sessions=sessions,
        app_logger=app_logger,
        public_origin="https://example.test",
    )
    return store, passwords, app_logger, sessions, invitations, resets


def _enroll(invitations: InvitationService, username: str = "alice"):
    issued = invitations.issue(username=username, display_name="Alice", roles=["user"])
    return invitations.accept(
        raw_token=issued.raw_token,
        password="correct horse battery staple",
        request_id="req_enroll",
    )


def test_invalid_invitation_and_reset_do_not_consume_password_work(tmp_path: Path):
    _store, passwords, _logger, _sessions, invitations, resets = _services(tmp_path)

    with pytest.raises(InvitationInvalidError):
        invitations.accept(
            raw_token=new_token("invitation"),
            password="correct horse battery staple",
        )
    with pytest.raises(PasswordResetInvalidError):
        resets.accept(
            raw_token=new_token("password_reset"),
            new_password="correct horse battery staple",
        )

    assert passwords.hash_calls == 0


def test_login_uses_dummy_work_and_never_logs_raw_session_identity(tmp_path: Path):
    marker = "correct horse battery marker"
    store, passwords, logger, sessions, invitations, _resets = _services(tmp_path)
    enrolled = _enroll(invitations)

    with pytest.raises(AuthInvalidCredentialsError):
        sessions.login(username="missing_user", password=marker, request_id="req_missing")
    assert passwords.dummy_calls == 1

    logged_in = sessions.login(
        username="alice",
        password="correct horse battery staple",
        request_id="req_login",
    )
    authenticated = sessions.resolve_session_token(
        token=logged_in.raw_session_token,
        request_id="req_resolve",
    )
    assert authenticated.user_context.metadata == {"auth_method": "web_session"}
    assert "web_session_id" not in authenticated.user_context.metadata

    serialized_logs = json.dumps(
        [record.__dict__ for record in logger.records], default=str, sort_keys=True
    )
    assert enrolled.raw_session_token not in serialized_logs
    assert logged_in.raw_session_token not in serialized_logs
    assert logged_in.session.id not in serialized_logs
    assert authenticated.correlation_digest in serialized_logs
    assert (
        store.browser_auth.resolve_session(
            token_digest=_stored_digest(store.db_path, logged_in.session.id)
        )
        is not None
    )


def test_legacy_invalid_username_cannot_browser_login_even_with_credential(
    tmp_path: Path,
):
    store, passwords, _logger, sessions, _invitations, _resets = _services(tmp_path)
    store.upsert_account(
        account_id="legacy_uppercase",
        username="LegacyUser",
        display_name="Legacy",
        status="active",
        roles=["user"],
    )
    connection = sqlite3.connect(store.db_path)
    try:
        connection.execute(
            """
            INSERT INTO account_credentials (
                account_id, password_hash, credential_version,
                created_at, updated_at, password_changed_at
            ) VALUES ('legacy_uppercase', 'fast:correct horse battery staple', 1,
                      't', 't', 't')
            """
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(AuthInvalidCredentialsError):
        sessions.login(
            username="legacyuser",
            password="correct horse battery staple",
        )
    assert passwords.dummy_calls == 1
    assert passwords.verify_calls == 0


def test_every_session_resolution_checks_current_credential_version(tmp_path: Path):
    store, _passwords, _logger, sessions, invitations, _resets = _services(tmp_path)
    enrolled = _enroll(invitations)

    connection = sqlite3.connect(store.db_path)
    try:
        connection.execute(
            "UPDATE account_credentials SET credential_version = 2 WHERE account_id = ?",
            (enrolled.account.id,),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(AuthSessionRevokedError):
        sessions.resolve_session_token(token=enrolled.raw_session_token)
    state = store.browser_auth.resolve_session(
        token_digest=_stored_digest(store.db_path, enrolled.session.id)
    )
    assert state is not None
    assert state.session.revoke_reason == "credential_version_mismatch"


def test_reset_enrollment_auto_login_and_password_change_rotation(tmp_path: Path):
    store, _passwords, _logger, sessions, _invitations, resets = _services(tmp_path)
    store.upsert_account(
        account_id="legacy_user",
        username="legacy_user",
        display_name="Legacy",
        status="active",
        roles=["user"],
    )
    issued = resets.issue(account_selector="legacy_user")
    assert issued.record.issued_credential_version == 0
    enrolled = resets.accept(
        raw_token=issued.raw_token,
        new_password="first browser password",
    )
    assert enrolled.session.authenticated_credential_version == 1
    authenticated = sessions.resolve_session_token(token=enrolled.raw_session_token)

    with pytest.raises(AuthInvalidCredentialsError):
        sessions.change_password(
            authenticated=authenticated,
            current_password="short",
            new_password="second browser password",
        )
    rotated = sessions.change_password(
        authenticated=authenticated,
        current_password="first browser password",
        new_password="second browser password",
    )
    assert rotated.session.authenticated_credential_version == 2
    with pytest.raises(AuthSessionRevokedError):
        sessions.resolve_session_token(token=enrolled.raw_session_token)
    resolved = sessions.resolve_session_token(token=rotated.raw_session_token)
    assert resolved.account.id == "legacy_user"


def test_expiry_is_fail_closed_and_logging_failure_does_not_change_commit(tmp_path: Path):
    now = datetime(2026, 8, 4, tzinfo=timezone.utc)
    clock_value = [now]
    _store, _passwords, _logger, sessions, invitations, _resets = _services(
        tmp_path, logger=FailingLogger(), clock=lambda: clock_value[0]
    )

    enrolled = _enroll(invitations)
    assert sessions.resolve_session_token(token=enrolled.raw_session_token).account.id
    clock_value[0] = now + timedelta(seconds=601)
    from app.auth.errors import AuthSessionExpiredError

    with pytest.raises(AuthSessionExpiredError):
        sessions.resolve_session_token(token=enrolled.raw_session_token)


def _stored_digest(path: Path, session_id: str) -> str:
    connection = sqlite3.connect(path)
    try:
        row = connection.execute(
            "SELECT token_digest FROM web_auth_sessions WHERE id = ?", (session_id,)
        ).fetchone()
        assert row is not None
        return str(row[0])
    finally:
        connection.close()


def test_auth_domain_modules_do_not_import_database_adapters():
    auth_root = Path(__file__).parents[1] / "app" / "auth"
    violations: list[str] = []
    for path in sorted(auth_root.glob("*.py")):
        if path.name == "admin.py":  # CLI composition root selects the concrete store.
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("app.db"):
                violations.append(f"{path.name}:{node.lineno}:{node.module}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("app.db"):
                        violations.append(f"{path.name}:{node.lineno}:{alias.name}")

    assert violations == []
