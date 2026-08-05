from __future__ import annotations

import hmac
import json
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Iterator, Literal

from app.accounts.schemas import AccountRecord
from app.auth.passwords import legacy_username_is_browser_eligible
from app.auth.schemas import (
    AccountInvitationRecord,
    AccountPasswordResetRecord,
    InvitationPreflight,
    LoginIdentity,
    PasswordResetPreflight,
    ResolvedSessionState,
    SessionMaterial,
    StoreSchemaInfo,
    WebAuthSessionRecord,
)
from app.auth.tokens import digest_matches
from app.db.browser_auth_migrations import connect_auth_database, store_schema_info

AcceptanceOutcome = Literal["accepted", "invalid", "identity_conflict"]


class BrowserAuthRepository:
    def __init__(self, *, db_path: Path, backend: str):
        self.db_path = db_path
        self.backend = backend

    def _connect(self) -> sqlite3.Connection:
        return connect_auth_database(self.db_path)

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def get_store_schema_info(self) -> StoreSchemaInfo:
        return store_schema_info(self.db_path, backend=self.backend)

    def get_login_identity(self, *, username_normalized: str) -> LoginIdentity | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT a.*, c.password_hash, c.credential_version
                FROM accounts AS a
                JOIN account_credentials AS c ON c.account_id = a.id
                WHERE a.username_normalized = ?
                LIMIT 1
                """,
                (username_normalized,),
            ).fetchone()
        if row is None:
            return None
        return LoginIdentity(
            account=_account_from_row(row),
            password_hash=row["password_hash"],
            credential_version=int(row["credential_version"]),
        )

    def resolve_account_id(self, *, account_selector: str) -> str | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT id
                FROM accounts
                WHERE id = ? OR username_normalized = ?
                ORDER BY CASE WHEN id = ? THEN 0 ELSE 1 END
                LIMIT 1
                """,
                (account_selector, account_selector, account_selector),
            ).fetchone()
        return str(row["id"]) if row is not None else None

    def create_login_session(
        self,
        *,
        account_id: str,
        expected_password_hash: str,
        expected_credential_version: int,
        replacement_password_hash: str | None,
        material: SessionMaterial,
    ) -> WebAuthSessionRecord | None:
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT a.status, c.password_hash, c.credential_version
                FROM accounts AS a
                JOIN account_credentials AS c ON c.account_id = a.id
                WHERE a.id = ?
                """,
                (account_id,),
            ).fetchone()
            if (
                row is None
                or row["status"] != "active"
                or int(row["credential_version"]) != expected_credential_version
                or not hmac.compare_digest(row["password_hash"], expected_password_hash)
            ):
                return None
            if replacement_password_hash is not None:
                changed = connection.execute(
                    """
                    UPDATE account_credentials
                    SET password_hash = ?, updated_at = ?
                    WHERE account_id = ? AND password_hash = ? AND credential_version = ?
                    """,
                    (
                        replacement_password_hash,
                        material.created_at,
                        account_id,
                        expected_password_hash,
                        expected_credential_version,
                    ),
                ).rowcount
                if changed != 1:
                    return None
            _insert_session(
                connection,
                account_id=account_id,
                credential_version=expected_credential_version,
                material=material,
            )
            connection.execute(
                "UPDATE accounts SET last_login_at = ?, updated_at = ? WHERE id = ?",
                (material.created_at, material.created_at, account_id),
            )
        return _session_from_material(
            account_id=account_id,
            credential_version=expected_credential_version,
            material=material,
        )

    def issue_invitation(
        self,
        *,
        invitation_id: str,
        token_digest: str,
        username: str,
        display_name: str | None,
        roles: list[str],
        created_at: str,
        expires_at: str,
    ) -> AccountInvitationRecord | None:
        with self._transaction() as connection:
            if connection.execute(
                "SELECT 1 FROM accounts WHERE username_normalized = ?",
                (username,),
            ).fetchone():
                return None
            connection.execute(
                """
                UPDATE account_invitations
                SET status = 'revoked', revoked_at = ?, updated_at = ?
                WHERE username_normalized = ? AND status = 'pending'
                """,
                (created_at, created_at, username),
            )
            connection.execute(
                """
                INSERT INTO account_invitations (
                    id, token_digest, username, username_normalized, display_name,
                    roles_json, status, created_at, updated_at, expires_at,
                    used_at, revoked_at, accepted_account_id
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, NULL, NULL, NULL)
                """,
                (
                    invitation_id,
                    token_digest,
                    username,
                    username,
                    display_name,
                    json.dumps(roles, ensure_ascii=False),
                    created_at,
                    created_at,
                    expires_at,
                ),
            )
            row = connection.execute(
                "SELECT * FROM account_invitations WHERE id = ?", (invitation_id,)
            ).fetchone()
        return _invitation_from_row(row)

    def preflight_invitation(self, *, token_digest: str, now: str) -> InvitationPreflight | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT id, token_digest, updated_at
                FROM account_invitations
                WHERE token_digest = ? AND status = 'pending' AND expires_at > ?
                LIMIT 1
                """,
                (token_digest, now),
            ).fetchone()
        if row is None or not digest_matches(row["token_digest"], token_digest):
            return None
        return InvitationPreflight(invitation_id=row["id"], updated_at=row["updated_at"])

    def accept_invitation(
        self,
        *,
        invitation_id: str,
        token_digest: str,
        password_hash: str,
        account_id: str,
        material: SessionMaterial,
        now: str,
    ) -> tuple[AcceptanceOutcome, AccountRecord | None, WebAuthSessionRecord | None]:
        with self._transaction() as connection:
            invitation = connection.execute(
                "SELECT * FROM account_invitations WHERE id = ?",
                (invitation_id,),
            ).fetchone()
            if (
                invitation is None
                or invitation["status"] != "pending"
                or invitation["expires_at"] <= now
                or not digest_matches(invitation["token_digest"], token_digest)
            ):
                return "invalid", None, None
            identity_exists = connection.execute(
                "SELECT 1 FROM accounts WHERE username_normalized = ?",
                (invitation["username_normalized"],),
            ).fetchone()
            if identity_exists is not None:
                changed = connection.execute(
                    """
                    UPDATE account_invitations
                    SET status = 'revoked', revoked_at = ?, updated_at = ?
                    WHERE id = ? AND status = 'pending'
                    """,
                    (now, now, invitation_id),
                ).rowcount
                if changed != 1:
                    return "invalid", None, None
                return "identity_conflict", None, None

            connection.execute(
                """
                INSERT INTO accounts (
                    id, username, username_normalized, display_name, status, roles_json,
                    dev_token_hash, created_at, updated_at, last_login_at
                ) VALUES (?, ?, ?, ?, 'active', ?, NULL, ?, ?, ?)
                """,
                (
                    account_id,
                    invitation["username"],
                    invitation["username_normalized"],
                    invitation["display_name"],
                    invitation["roles_json"],
                    now,
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO account_credentials (
                    account_id, password_hash, credential_version,
                    created_at, updated_at, password_changed_at
                ) VALUES (?, ?, 1, ?, ?, ?)
                """,
                (account_id, password_hash, now, now, now),
            )
            _insert_session(
                connection,
                account_id=account_id,
                credential_version=1,
                material=material,
            )
            changed = connection.execute(
                """
                UPDATE account_invitations
                SET status = 'used', used_at = ?, updated_at = ?, accepted_account_id = ?
                WHERE id = ? AND status = 'pending'
                """,
                (now, now, account_id, invitation_id),
            ).rowcount
            if changed != 1:
                raise sqlite3.IntegrityError("invitation_claim_lost")
            account_row = connection.execute(
                "SELECT * FROM accounts WHERE id = ?", (account_id,)
            ).fetchone()
        return (
            "accepted",
            _account_from_row(account_row),
            _session_from_material(account_id=account_id, credential_version=1, material=material),
        )

    def list_invitations(self, *, limit: int = 50) -> list[AccountInvitationRecord]:
        bounded = max(1, min(limit, 200))
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM account_invitations ORDER BY created_at DESC, id DESC LIMIT ?",
                (bounded,),
            ).fetchall()
        return [_invitation_from_row(row) for row in rows]

    def revoke_invitation(self, *, invitation_id: str, now: str) -> bool:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT status FROM account_invitations WHERE id = ?", (invitation_id,)
            ).fetchone()
            if row is None:
                return False
            if row["status"] == "used":
                raise ValueError("used_invitation_cannot_be_revoked")
            if row["status"] == "revoked":
                return False
            return bool(
                connection.execute(
                    """
                    UPDATE account_invitations
                    SET status = 'revoked', revoked_at = ?, updated_at = ?
                    WHERE id = ? AND status = 'pending'
                    """,
                    (now, now, invitation_id),
                ).rowcount
            )

    def issue_password_reset(
        self,
        *,
        account_selector: str,
        reset_id: str,
        token_digest: str,
        created_at: str,
        expires_at: str,
    ) -> AccountPasswordResetRecord | None:
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT a.id, a.username, a.status, c.credential_version
                FROM accounts AS a
                LEFT JOIN account_credentials AS c ON c.account_id = a.id
                WHERE a.id = ? OR a.username_normalized = ?
                ORDER BY CASE WHEN a.id = ? THEN 0 ELSE 1 END
                LIMIT 1
                """,
                (account_selector, account_selector, account_selector),
            ).fetchone()
            if row is None or row["status"] != "active":
                return None
            if row["credential_version"] is None:
                if not legacy_username_is_browser_eligible(row["username"]):
                    return None
                version = 0
            else:
                version = int(row["credential_version"])
            connection.execute(
                """
                UPDATE account_password_resets
                SET status = 'revoked', revoked_at = ?, updated_at = ?
                WHERE account_id = ? AND status = 'pending'
                """,
                (created_at, created_at, row["id"]),
            )
            connection.execute(
                """
                INSERT INTO account_password_resets (
                    id, account_id, token_digest, issued_credential_version, status,
                    created_at, updated_at, expires_at, used_at, revoked_at
                ) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, NULL, NULL)
                """,
                (
                    reset_id,
                    row["id"],
                    token_digest,
                    version,
                    created_at,
                    created_at,
                    expires_at,
                ),
            )
            reset = connection.execute(
                "SELECT * FROM account_password_resets WHERE id = ?", (reset_id,)
            ).fetchone()
        return _password_reset_from_row(reset)

    def preflight_password_reset(
        self, *, token_digest: str, now: str
    ) -> PasswordResetPreflight | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT r.*, a.username, a.status AS account_status,
                       c.credential_version AS current_version
                FROM account_password_resets AS r
                JOIN accounts AS a ON a.id = r.account_id
                LEFT JOIN account_credentials AS c ON c.account_id = a.id
                WHERE r.token_digest = ? AND r.status = 'pending' AND r.expires_at > ?
                LIMIT 1
                """,
                (token_digest, now),
            ).fetchone()
        if row is None or not digest_matches(row["token_digest"], token_digest):
            return None
        if row["account_status"] != "active":
            return None
        issued = int(row["issued_credential_version"])
        current = row["current_version"]
        if issued == 0:
            if current is not None or not legacy_username_is_browser_eligible(row["username"]):
                return None
        elif current is None or int(current) != issued:
            return None
        return PasswordResetPreflight(
            reset_id=row["id"],
            account_id=row["account_id"],
            issued_credential_version=issued,
            updated_at=row["updated_at"],
        )

    def accept_password_reset(
        self,
        *,
        reset_id: str,
        token_digest: str,
        password_hash: str,
        material: SessionMaterial,
        now: str,
    ) -> tuple[bool, AccountRecord | None, WebAuthSessionRecord | None]:
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT r.*, a.username, a.status AS account_status,
                       c.credential_version AS current_version
                FROM account_password_resets AS r
                JOIN accounts AS a ON a.id = r.account_id
                LEFT JOIN account_credentials AS c ON c.account_id = a.id
                WHERE r.id = ?
                """,
                (reset_id,),
            ).fetchone()
            if (
                row is None
                or row["status"] != "pending"
                or row["expires_at"] <= now
                or row["account_status"] != "active"
                or not digest_matches(row["token_digest"], token_digest)
            ):
                return False, None, None
            issued = int(row["issued_credential_version"])
            current = row["current_version"]
            if issued == 0:
                if current is not None or not legacy_username_is_browser_eligible(row["username"]):
                    return False, None, None
                new_version = 1
                connection.execute(
                    """
                    INSERT INTO account_credentials (
                        account_id, password_hash, credential_version,
                        created_at, updated_at, password_changed_at
                    ) VALUES (?, ?, 1, ?, ?, ?)
                    """,
                    (row["account_id"], password_hash, now, now, now),
                )
            else:
                if current is None or int(current) != issued:
                    return False, None, None
                new_version = issued + 1
                changed = connection.execute(
                    """
                    UPDATE account_credentials
                    SET password_hash = ?, credential_version = ?,
                        updated_at = ?, password_changed_at = ?
                    WHERE account_id = ? AND credential_version = ?
                    """,
                    (
                        password_hash,
                        new_version,
                        now,
                        now,
                        row["account_id"],
                        issued,
                    ),
                ).rowcount
                if changed != 1:
                    return False, None, None
            connection.execute(
                """
                UPDATE web_auth_sessions
                SET revoked_at = ?, revoke_reason = 'password_reset', updated_at = ?
                WHERE account_id = ? AND revoked_at IS NULL
                """,
                (now, now, row["account_id"]),
            )
            _insert_session(
                connection,
                account_id=row["account_id"],
                credential_version=new_version,
                material=material,
            )
            changed = connection.execute(
                """
                UPDATE account_password_resets
                SET status = 'used', used_at = ?, updated_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (now, now, reset_id),
            ).rowcount
            if changed != 1:
                raise sqlite3.IntegrityError("password_reset_claim_lost")
            account = connection.execute(
                "SELECT * FROM accounts WHERE id = ?", (row["account_id"],)
            ).fetchone()
        return (
            True,
            _account_from_row(account),
            _session_from_material(
                account_id=row["account_id"],
                credential_version=new_version,
                material=material,
            ),
        )

    def list_password_resets(self, *, limit: int = 50) -> list[AccountPasswordResetRecord]:
        bounded = max(1, min(limit, 200))
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM account_password_resets ORDER BY created_at DESC, id DESC LIMIT ?",
                (bounded,),
            ).fetchall()
        return [_password_reset_from_row(row) for row in rows]

    def resolve_session(self, *, token_digest: str) -> ResolvedSessionState | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT s.*, a.username, a.username_normalized, a.display_name,
                       a.status AS account_status, a.roles_json, a.dev_token_hash,
                       a.created_at AS account_created_at, a.updated_at AS account_updated_at,
                       a.last_login_at, c.credential_version AS current_credential_version
                FROM web_auth_sessions AS s
                JOIN accounts AS a ON a.id = s.account_id
                LEFT JOIN account_credentials AS c ON c.account_id = a.id
                WHERE s.token_digest = ?
                LIMIT 1
                """,
                (token_digest,),
            ).fetchone()
        if row is None or not digest_matches(row["token_digest"], token_digest):
            return None
        account = AccountRecord(
            id=row["account_id"],
            username=row["username"],
            username_normalized=row["username_normalized"],
            display_name=row["display_name"],
            status=row["account_status"],
            roles=json.loads(row["roles_json"]),
            dev_token_hash=row["dev_token_hash"],
            created_at=row["account_created_at"],
            updated_at=row["account_updated_at"],
            last_login_at=row["last_login_at"],
        )
        return ResolvedSessionState(
            account=account,
            session=_web_session_from_row(row),
            csrf_verifier=bytes(row["csrf_verifier"]),
            current_credential_version=(
                int(row["current_credential_version"])
                if row["current_credential_version"] is not None
                else None
            ),
        )

    def revoke_session(self, *, session_id: str, reason: str, now: str) -> bool:
        with self._transaction() as connection:
            return bool(
                connection.execute(
                    """
                    UPDATE web_auth_sessions
                    SET revoked_at = ?, revoke_reason = ?, updated_at = ?
                    WHERE id = ? AND revoked_at IS NULL
                    """,
                    (now, reason, now, session_id),
                ).rowcount
            )

    def touch_session(
        self,
        *,
        session_id: str,
        observed_last_seen: str,
        now: str,
        idle_expires_at: str,
    ) -> bool:
        with self._transaction() as connection:
            return bool(
                connection.execute(
                    """
                    UPDATE web_auth_sessions
                    SET last_seen_at = ?, idle_expires_at = ?, updated_at = ?
                    WHERE id = ? AND revoked_at IS NULL AND last_seen_at = ?
                    """,
                    (now, idle_expires_at, now, session_id, observed_last_seen),
                ).rowcount
            )

    def revoke_account_sessions(self, *, account_id: str, reason: str, now: str) -> int:
        with self._transaction() as connection:
            return int(
                connection.execute(
                    """
                    UPDATE web_auth_sessions
                    SET revoked_at = ?, revoke_reason = ?, updated_at = ?
                    WHERE account_id = ? AND revoked_at IS NULL
                    """,
                    (now, reason, now, account_id),
                ).rowcount
            )

    def change_password_and_rotate_session(
        self,
        *,
        account_id: str,
        expected_password_hash: str,
        expected_credential_version: int,
        new_password_hash: str,
        material: SessionMaterial,
        now: str,
    ) -> WebAuthSessionRecord | None:
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT a.status, c.password_hash, c.credential_version
                FROM accounts AS a
                JOIN account_credentials AS c ON c.account_id = a.id
                WHERE a.id = ?
                """,
                (account_id,),
            ).fetchone()
            if (
                row is None
                or row["status"] != "active"
                or int(row["credential_version"]) != expected_credential_version
                or not hmac.compare_digest(row["password_hash"], expected_password_hash)
            ):
                return None
            new_version = expected_credential_version + 1
            changed = connection.execute(
                """
                UPDATE account_credentials
                SET password_hash = ?, credential_version = ?,
                    updated_at = ?, password_changed_at = ?
                WHERE account_id = ? AND password_hash = ? AND credential_version = ?
                """,
                (
                    new_password_hash,
                    new_version,
                    now,
                    now,
                    account_id,
                    expected_password_hash,
                    expected_credential_version,
                ),
            ).rowcount
            if changed != 1:
                return None
            connection.execute(
                """
                UPDATE web_auth_sessions
                SET revoked_at = ?, revoke_reason = 'password_change', updated_at = ?
                WHERE account_id = ? AND revoked_at IS NULL
                """,
                (now, now, account_id),
            )
            _insert_session(
                connection,
                account_id=account_id,
                credential_version=new_version,
                material=material,
            )
        return _session_from_material(
            account_id=account_id,
            credential_version=new_version,
            material=material,
        )

    def disable_account(self, *, account_id: str, now: str) -> bool:
        with self._transaction() as connection:
            changed = connection.execute(
                "UPDATE accounts SET status = 'disabled', updated_at = ? WHERE id = ?",
                (now, account_id),
            ).rowcount
            if not changed:
                return False
            connection.execute(
                """
                UPDATE web_auth_sessions
                SET revoked_at = ?, revoke_reason = 'account_disabled', updated_at = ?
                WHERE account_id = ? AND revoked_at IS NULL
                """,
                (now, now, account_id),
            )
            connection.execute(
                """
                UPDATE account_password_resets
                SET status = 'revoked', revoked_at = ?, updated_at = ?
                WHERE account_id = ? AND status = 'pending'
                """,
                (now, now, account_id),
            )
            return True

    def enable_account(self, *, account_id: str, now: str) -> bool:
        with self._transaction() as connection:
            return bool(
                connection.execute(
                    "UPDATE accounts SET status = 'active', updated_at = ? WHERE id = ?",
                    (now, account_id),
                ).rowcount
            )

    def cleanup_sessions(self, *, before: str, limit: int) -> int:
        bounded = max(1, min(limit, 1000))
        with self._transaction() as connection:
            ids = [
                row["id"]
                for row in connection.execute(
                    """
                    SELECT id FROM web_auth_sessions
                    WHERE absolute_expires_at <= ? OR (revoked_at IS NOT NULL AND revoked_at <= ?)
                    ORDER BY absolute_expires_at, id LIMIT ?
                    """,
                    (before, before, bounded),
                ).fetchall()
            ]
            if not ids:
                return 0
            placeholders = ",".join("?" for _ in ids)
            return int(
                connection.execute(
                    f"DELETE FROM web_auth_sessions WHERE id IN ({placeholders})", ids
                ).rowcount
            )


def _insert_session(
    connection: sqlite3.Connection,
    *,
    account_id: str,
    credential_version: int,
    material: SessionMaterial,
) -> None:
    connection.execute(
        """
        INSERT INTO web_auth_sessions (
            id, account_id, token_digest, csrf_verifier,
            authenticated_credential_version, created_at, updated_at,
            last_seen_at, idle_expires_at, absolute_expires_at,
            revoked_at, revoke_reason
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)
        """,
        (
            material.session_id,
            account_id,
            material.token_digest,
            material.csrf_verifier,
            credential_version,
            material.created_at,
            material.created_at,
            material.last_seen_at,
            material.idle_expires_at,
            material.absolute_expires_at,
        ),
    )


def _account_from_row(row: sqlite3.Row) -> AccountRecord:
    return AccountRecord(
        id=row["id"],
        username=row["username"],
        username_normalized=row["username_normalized"],
        display_name=row["display_name"],
        status=row["status"],
        roles=json.loads(row["roles_json"]),
        dev_token_hash=row["dev_token_hash"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        last_login_at=row["last_login_at"],
    )


def _invitation_from_row(row: sqlite3.Row) -> AccountInvitationRecord:
    return AccountInvitationRecord(
        id=row["id"],
        username=row["username"],
        username_normalized=row["username_normalized"],
        display_name=row["display_name"],
        roles=[str(item) for item in json.loads(row["roles_json"])],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        expires_at=row["expires_at"],
        used_at=row["used_at"],
        revoked_at=row["revoked_at"],
        accepted_account_id=row["accepted_account_id"],
    )


def _password_reset_from_row(row: sqlite3.Row) -> AccountPasswordResetRecord:
    return AccountPasswordResetRecord(
        id=row["id"],
        account_id=row["account_id"],
        issued_credential_version=int(row["issued_credential_version"]),
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        expires_at=row["expires_at"],
        used_at=row["used_at"],
        revoked_at=row["revoked_at"],
    )


def _web_session_from_row(row: sqlite3.Row) -> WebAuthSessionRecord:
    return WebAuthSessionRecord(
        id=row["id"],
        account_id=row["account_id"],
        authenticated_credential_version=int(row["authenticated_credential_version"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        last_seen_at=row["last_seen_at"],
        idle_expires_at=row["idle_expires_at"],
        absolute_expires_at=row["absolute_expires_at"],
        revoked_at=row["revoked_at"],
        revoke_reason=row["revoke_reason"],
    )


def _session_from_material(
    *, account_id: str, credential_version: int, material: SessionMaterial
) -> WebAuthSessionRecord:
    return WebAuthSessionRecord(
        id=material.session_id,
        account_id=account_id,
        authenticated_credential_version=credential_version,
        created_at=material.created_at,
        updated_at=material.created_at,
        last_seen_at=material.last_seen_at,
        idle_expires_at=material.idle_expires_at,
        absolute_expires_at=material.absolute_expires_at,
        revoked_at=None,
        revoke_reason=None,
    )
