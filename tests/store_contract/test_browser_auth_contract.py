from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from app.auth.schemas import SessionMaterial
from app.auth.tokens import new_token, token_digest

NOW = datetime(2026, 8, 4, tzinfo=timezone.utc)


def _time(offset_seconds: int = 0) -> str:
    return (NOW + timedelta(seconds=offset_seconds)).isoformat(timespec="microseconds")


def _material(suffix: str, *, offset_seconds: int = 0) -> SessionMaterial:
    raw = new_token("web_session")
    return SessionMaterial(
        session_id=f"wsa_{suffix}",
        raw_token=raw,
        token_digest=token_digest(raw, kind="web_session"),
        csrf_verifier=b"c" * 32,
        created_at=_time(offset_seconds),
        last_seen_at=_time(offset_seconds),
        idle_expires_at=_time(offset_seconds + 600),
        absolute_expires_at=_time(offset_seconds + 3600),
    )


def _issue(store, *, invitation_id: str, username: str, token: str):
    return store.browser_auth.issue_invitation(
        invitation_id=invitation_id,
        token_digest=token_digest(token, kind="invitation"),
        username=username,
        display_name=username.title(),
        roles=["user"],
        created_at=_time(),
        expires_at=_time(3600),
    )


def test_schema_info_and_constraints_contract(store):
    info = store.get_store_schema_info()
    assert info.db_backend in {"sqlite_raw", "sqlalchemy"}
    assert "browser_auth_v0_1" in info.migration_ids
    assert info.browser_auth_schema_revision == "browser_auth_v0_1"
    assert len(info.schema_contract_fingerprint) == 64

    connection = sqlite3.connect(store.db_path)
    try:
        with connection:
            try:
                connection.execute(
                    """
                    INSERT INTO web_auth_sessions (
                        id, account_id, token_digest, csrf_verifier,
                        authenticated_credential_version, created_at, updated_at,
                        last_seen_at, idle_expires_at, absolute_expires_at
                    ) VALUES ('bad', 'user_default', 'bad', ?, 0, 't', 't', 't', 't', 't')
                    """,
                    (b"c" * 32,),
                )
            except sqlite3.IntegrityError as exc:
                assert "CHECK constraint failed" in str(exc)
            else:
                raise AssertionError("credential version CHECK was not enforced")
    finally:
        connection.close()


def test_invitation_reissue_accept_replay_and_session_resolution(store):
    first_token = new_token("invitation")
    second_token = new_token("invitation")
    first = _issue(store, invitation_id="inv_first", username="alice", token=first_token)
    second = _issue(store, invitation_id="inv_second", username="alice", token=second_token)

    assert first is not None and second is not None
    records = {item.id: item for item in store.browser_auth.list_invitations()}
    assert records["inv_first"].status == "revoked"
    assert records["inv_second"].status == "pending"
    assert (
        store.browser_auth.preflight_invitation(
            token_digest=token_digest(first_token, kind="invitation"), now=_time()
        )
        is None
    )

    digest = token_digest(second_token, kind="invitation")
    outcome, account, session = store.browser_auth.accept_invitation(
        invitation_id="inv_second",
        token_digest=digest,
        password_hash="argon-hash",
        account_id="account_alice",
        material=_material("alice"),
        now=_time(),
    )
    assert outcome == "accepted"
    assert account is not None and account.username_normalized == "alice"
    assert session is not None and session.authenticated_credential_version == 1
    state = store.browser_auth.resolve_session(token_digest=_material_digest(store, "wsa_alice"))
    assert state is not None and state.current_credential_version == 1

    replay = store.browser_auth.accept_invitation(
        invitation_id="inv_second",
        token_digest=digest,
        password_hash="other-hash",
        account_id="account_other",
        material=_material("replay"),
        now=_time(),
    )
    assert replay == ("invalid", None, None)


def _material_digest(store, session_id: str) -> str:
    connection = sqlite3.connect(store.db_path)
    try:
        row = connection.execute(
            "SELECT token_digest FROM web_auth_sessions WHERE id = ?", (session_id,)
        ).fetchone()
        assert row is not None
        return str(row[0])
    finally:
        connection.close()


def test_invitation_identity_conflict_revokes_without_partial_auth_rows(store):
    raw_token = new_token("invitation")
    assert _issue(store, invitation_id="inv_conflict", username="bob", token=raw_token)
    store.upsert_account(
        account_id="account_bob",
        username="bob",
        display_name="Bob",
        status="active",
        roles=["user"],
    )

    outcome = store.browser_auth.accept_invitation(
        invitation_id="inv_conflict",
        token_digest=token_digest(raw_token, kind="invitation"),
        password_hash="must-not-persist",
        account_id="must_not_exist",
        material=_material("conflict"),
        now=_time(),
    )

    assert outcome == ("identity_conflict", None, None)
    invitation = next(
        item for item in store.browser_auth.list_invitations() if item.id == "inv_conflict"
    )
    assert invitation.status == "revoked"
    assert store.get_account(account_id="must_not_exist") is None


def test_reset_version_zero_enrollment_and_version_rotation(store):
    store.upsert_account(
        account_id="legacy_account",
        username="legacy_account",
        display_name="Legacy",
        status="active",
        roles=["user"],
    )
    raw = new_token("password_reset")
    reset = store.browser_auth.issue_password_reset(
        account_selector="legacy_account",
        reset_id="rst_enroll",
        token_digest=token_digest(raw, kind="password_reset"),
        created_at=_time(),
        expires_at=_time(3600),
    )
    assert reset is not None and reset.issued_credential_version == 0
    accepted, account, first_session = store.browser_auth.accept_password_reset(
        reset_id=reset.id,
        token_digest=token_digest(raw, kind="password_reset"),
        password_hash="hash-v1",
        material=_material("enroll"),
        now=_time(),
    )
    assert accepted and account is not None and first_session is not None
    assert first_session.authenticated_credential_version == 1

    second_raw = new_token("password_reset")
    second = store.browser_auth.issue_password_reset(
        account_selector="legacy_account",
        reset_id="rst_rotate",
        token_digest=token_digest(second_raw, kind="password_reset"),
        created_at=_time(1),
        expires_at=_time(3601),
    )
    assert second is not None and second.issued_credential_version == 1
    accepted, _account, fresh = store.browser_auth.accept_password_reset(
        reset_id=second.id,
        token_digest=token_digest(second_raw, kind="password_reset"),
        password_hash="hash-v2",
        material=_material("rotated", offset_seconds=1),
        now=_time(1),
    )
    assert accepted and fresh is not None
    assert fresh.authenticated_credential_version == 2

    old = store.browser_auth.resolve_session(token_digest=_material_digest(store, "wsa_enroll"))
    assert old is not None and old.session.revoke_reason == "password_reset"
    current = store.browser_auth.resolve_session(
        token_digest=_material_digest(store, "wsa_rotated")
    )
    assert current is not None and current.current_credential_version == 2


def test_login_session_cas_and_concurrent_invitation_accept(store):
    raw = new_token("invitation")
    assert _issue(store, invitation_id="inv_race", username="carol", token=raw)
    digest = token_digest(raw, kind="invitation")

    def accept(index: int):
        return store.browser_auth.accept_invitation(
            invitation_id="inv_race",
            token_digest=digest,
            password_hash="hash-v1",
            account_id=f"account_carol_{index}",
            material=_material(f"carol_{index}"),
            now=_time(),
        )[0]

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(accept, (1, 2)))
    assert outcomes.count("accepted") == 1
    assert outcomes.count("invalid") == 1

    identity = store.browser_auth.get_login_identity(username_normalized="carol")
    assert identity is not None
    connection = sqlite3.connect(store.db_path)
    try:
        connection.execute(
            "UPDATE account_credentials SET password_hash = 'raced-hash', "
            "credential_version = 2 WHERE account_id = ?",
            (identity.account.id,),
        )
        connection.commit()
    finally:
        connection.close()
    stale = store.browser_auth.create_login_session(
        account_id=identity.account.id,
        expected_password_hash=identity.password_hash,
        expected_credential_version=identity.credential_version,
        replacement_password_hash=None,
        material=_material("stale"),
    )
    assert stale is None


def test_concurrent_reset_accept_has_one_winner(store):
    store.upsert_account(
        account_id="reset_race_account",
        username="reset_race_user",
        display_name="Reset Race",
        status="active",
        roles=["user"],
    )
    raw = new_token("password_reset")
    reset = store.browser_auth.issue_password_reset(
        account_selector="reset_race_account",
        reset_id="rst_race",
        token_digest=token_digest(raw, kind="password_reset"),
        created_at=_time(),
        expires_at=_time(3600),
    )
    assert reset is not None

    def accept(index: int) -> bool:
        return store.browser_auth.accept_password_reset(
            reset_id=reset.id,
            token_digest=token_digest(raw, kind="password_reset"),
            password_hash=f"hash-{index}",
            material=_material(f"reset_race_{index}"),
            now=_time(),
        )[0]

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(accept, (1, 2)))
    assert outcomes.count(True) == 1
    assert outcomes.count(False) == 1


def test_disable_revoke_touch_cas_and_bounded_cleanup(store):
    raw = new_token("invitation")
    assert _issue(store, invitation_id="inv_ops", username="opsaccount", token=raw)
    outcome, account, first = store.browser_auth.accept_invitation(
        invitation_id="inv_ops",
        token_digest=token_digest(raw, kind="invitation"),
        password_hash="ops-hash",
        account_id="ops_account",
        material=_material("ops_initial", offset_seconds=-10_000),
        now=_time(-10_000),
    )
    assert outcome == "accepted" and account is not None and first is not None
    identity = store.browser_auth.get_login_identity(username_normalized="opsaccount")
    assert identity is not None
    for index in range(3):
        assert store.browser_auth.create_login_session(
            account_id=account.id,
            expected_password_hash=identity.password_hash,
            expected_credential_version=identity.credential_version,
            replacement_password_hash=None,
            material=_material(f"old_{index}", offset_seconds=-10_000 + index),
        )

    active = store.browser_auth.create_login_session(
        account_id=account.id,
        expected_password_hash=identity.password_hash,
        expected_credential_version=identity.credential_version,
        replacement_password_hash=None,
        material=_material("active"),
    )
    assert active is not None
    assert store.browser_auth.touch_session(
        session_id=active.id,
        observed_last_seen=active.last_seen_at,
        now=_time(60),
        idle_expires_at=_time(660),
    )
    assert not store.browser_auth.touch_session(
        session_id=active.id,
        observed_last_seen=active.last_seen_at,
        now=_time(61),
        idle_expires_at=_time(661),
    )

    assert store.browser_auth.cleanup_sessions(before=_time(), limit=2) == 2
    assert store.browser_auth.cleanup_sessions(before=_time(), limit=2) == 2
    assert (
        store.browser_auth.resolve_session(token_digest=_material_digest(store, active.id))
        is not None
    )

    reset_raw = new_token("password_reset")
    reset = store.browser_auth.issue_password_reset(
        account_selector=account.id,
        reset_id="rst_disable",
        token_digest=token_digest(reset_raw, kind="password_reset"),
        created_at=_time(100),
        expires_at=_time(3700),
    )
    assert reset is not None
    assert store.browser_auth.disable_account(account_id=account.id, now=_time(101))
    state = store.browser_auth.resolve_session(token_digest=_material_digest(store, active.id))
    assert state is not None and state.session.revoke_reason == "account_disabled"
    reset_record = next(
        item for item in store.browser_auth.list_password_resets() if item.id == reset.id
    )
    assert reset_record.status == "revoked"
    assert store.browser_auth.enable_account(account_id=account.id, now=_time(102))
    state = store.browser_auth.resolve_session(token_digest=_material_digest(store, active.id))
    assert state is not None and state.session.revoked_at is not None
