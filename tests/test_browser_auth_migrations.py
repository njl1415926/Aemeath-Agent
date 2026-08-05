from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.db.browser_auth_migrations import (
    BROWSER_AUTH_MIGRATION_ID,
    _schema_contract_fingerprint,
    connect_auth_database,
    ensure_browser_auth_schema,
)
from app.db.errors import StorageMigrationRequiredError
from app.db.sqlalchemy_store import SqlAlchemyStore
from app.db.sqlite_store import SQLiteStore

LEGACY_ACCOUNTS_SQL = """
CREATE TABLE accounts (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    display_name TEXT,
    status TEXT NOT NULL,
    roles_json TEXT NOT NULL DEFAULT '["user"]',
    dev_token_hash TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_login_at TEXT
)
"""


def _legacy_database(path: Path, usernames: list[str]) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(LEGACY_ACCOUNTS_SQL)
        for index, username in enumerate(usernames):
            connection.execute(
                """
                INSERT INTO accounts (
                    id, username, display_name, status, roles_json,
                    created_at, updated_at
                ) VALUES (?, ?, NULL, 'active', '["user"]', ?, ?)
                """,
                (
                    f"account_{index}",
                    username,
                    "2026-01-01T00:00:00+00:00",
                    "2026-01-01T00:00:00+00:00",
                ),
            )
        connection.commit()
    finally:
        connection.close()


def test_legacy_migration_backfills_permanent_default_and_guards(tmp_path: Path):
    db_path = tmp_path / "legacy.db"
    _legacy_database(db_path, ["alice", "legacy-name"])

    ensure_browser_auth_schema(db_path)
    ensure_browser_auth_schema(db_path)

    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        column = next(
            row
            for row in connection.execute("PRAGMA table_xinfo('accounts')")
            if row["name"] == "username_normalized"
        )
        assert column["notnull"] == 1
        assert column["dflt_value"] == "''"
        assert [
            row[0]
            for row in connection.execute(
                "SELECT username_normalized FROM accounts ORDER BY id"
            ).fetchall()
        ] == ["alice", "legacy-name"]
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM schema_migrations WHERE version = ?",
                (BROWSER_AUTH_MIGRATION_ID,),
            ).fetchone()[0]
            == 1
        )
        triggers = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_schema WHERE type = 'trigger'")
        }
        assert triggers.issuperset(
            {
                "trg_accounts_username_normalized_insert_nonempty",
                "trg_accounts_username_normalized_update_nonempty",
                "trg_accounts_username_identity_immutable",
            }
        )
        with pytest.raises(sqlite3.IntegrityError, match="username_normalized_empty"):
            connection.execute(
                """
                INSERT INTO accounts (
                    id, username, display_name, status, roles_json,
                    created_at, updated_at
                ) VALUES ('bad', 'bad', NULL, 'active', '["user"]', 't', 't')
                """
            )
        with pytest.raises(sqlite3.IntegrityError, match="username_immutable"):
            connection.execute("UPDATE accounts SET username = 'changed' WHERE id = 'account_0'")
    finally:
        connection.close()


def test_migration_collision_fails_without_partial_schema(tmp_path: Path):
    db_path = tmp_path / "collision.db"
    _legacy_database(db_path, ["Alice", "alice"])

    with pytest.raises(StorageMigrationRequiredError) as raised:
        ensure_browser_auth_schema(db_path)

    assert raised.value.metadata["reason"] == "account_username_normalized_collision"
    connection = sqlite3.connect(db_path)
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info('accounts')")}
        assert "username_normalized" not in columns
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_schema WHERE name = 'account_credentials'"
            ).fetchone()
            is None
        )
    finally:
        connection.close()


def test_startup_fails_closed_on_trigger_drift_and_foreign_key_orphan(tmp_path: Path):
    trigger_path = tmp_path / "trigger-drift.db"
    store = SqlAlchemyStore(trigger_path)
    store.engine.dispose()
    connection = sqlite3.connect(trigger_path)
    try:
        connection.execute("DROP TRIGGER trg_accounts_username_normalized_update_nonempty")
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(StorageMigrationRequiredError) as trigger_error:
        ensure_browser_auth_schema(trigger_path)
    assert trigger_error.value.metadata["reason"] == "account_username_trigger_invalid"

    orphan_path = tmp_path / "orphan.db"
    store = SqlAlchemyStore(orphan_path)
    store.engine.dispose()
    connection = sqlite3.connect(orphan_path)
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            """
            INSERT INTO account_credentials (
                account_id, password_hash, credential_version,
                created_at, updated_at, password_changed_at
            ) VALUES ('missing', 'hash', 1, 't', 't', 't')
            """
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(StorageMigrationRequiredError) as fk_error:
        ensure_browser_auth_schema(orphan_path)
    assert fk_error.value.metadata["reason"] == "foreign_key_violation"


def test_startup_fails_closed_on_auth_index_drift(tmp_path: Path):
    db_path = tmp_path / "index-drift.db"
    store = SqlAlchemyStore(db_path)
    store.engine.dispose()
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("DROP INDEX idx_web_auth_sessions_account_active")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(StorageMigrationRequiredError) as raised:
        ensure_browser_auth_schema(db_path)
    assert raised.value.metadata["reason"] == "browser_auth_index_invalid"


def test_sqlalchemy_migrates_existing_accounts_and_auth_connections_enable_fk(tmp_path: Path):
    db_path = tmp_path / "sqlalchemy-existing.db"
    _legacy_database(db_path, ["alice"])

    store = SqlAlchemyStore(db_path)
    try:
        account = store.get_account(account_id="account_0")
        assert account is not None
        assert account.username_normalized == "alice"
        info = store.get_store_schema_info()
        assert info.browser_auth_schema_revision == "browser_auth_v0_1"
        assert len(info.schema_contract_fingerprint) == 64
    finally:
        store.engine.dispose()

    connection = connect_auth_database(db_path)
    try:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


_SCHEMA_DRIFT_CASES = {
    "partial_predicate_narrowed": (
        """
        CREATE UNIQUE INDEX uq_account_invitations_pending_username
        ON account_invitations (username_normalized)
        WHERE status = 'pending' AND 0
        """,
        "browser_auth_index_invalid",
    ),
    "partial_predicate_expanded": (
        """
        CREATE UNIQUE INDEX uq_account_invitations_pending_username
        ON account_invitations (username_normalized)
        WHERE status = 'pending' OR status = 'used'
        """,
        "browser_auth_index_invalid",
    ),
    "partial_predicate_literal_case": (
        """
        CREATE UNIQUE INDEX uq_account_invitations_pending_username
        ON account_invitations (username_normalized)
        WHERE status = 'Pending'
        """,
        "browser_auth_index_invalid",
    ),
    "trigger_and_zero": (
        """
        CREATE TRIGGER trg_accounts_username_identity_immutable
        BEFORE UPDATE OF username, username_normalized ON accounts
        WHEN (NEW.username <> OLD.username
          OR NEW.username_normalized <> OLD.username_normalized) AND 0
        BEGIN
            SELECT RAISE(ABORT, 'account_username_immutable');
        END
        """,
        "account_username_trigger_invalid",
    ),
    "trigger_literal_space": (
        """
        CREATE TRIGGER trg_accounts_username_identity_immutable
        BEFORE UPDATE OF username, username_normalized ON accounts
        WHEN NEW.username <> OLD.username
          OR NEW.username_normalized <> OLD.username_normalized
        BEGIN
            SELECT RAISE(ABORT, 'account_username_immutable ');
        END
        """,
        "account_username_trigger_invalid",
    ),
    "trigger_condition_changed": (
        """
        CREATE TRIGGER trg_accounts_username_identity_immutable
        BEFORE UPDATE OF username, username_normalized ON accounts
        WHEN NEW.username <> OLD.username
          AND NEW.username_normalized <> OLD.username_normalized
        BEGIN
            SELECT RAISE(ABORT, 'account_username_immutable');
        END
        """,
        "account_username_trigger_invalid",
    ),
}


@pytest.mark.parametrize("backend", ["sqlite_raw", "sqlalchemy"])
@pytest.mark.parametrize("drift", sorted(_SCHEMA_DRIFT_CASES))
def test_restart_rejects_semantic_schema_drift_and_changes_fingerprint(
    tmp_path: Path,
    backend: str,
    drift: str,
):
    db_path = tmp_path / f"{backend}-{drift}.db"
    store = SQLiteStore(db_path) if backend == "sqlite_raw" else SqlAlchemyStore(db_path)
    baseline_fingerprint = store.get_store_schema_info().schema_contract_fingerprint
    if isinstance(store, SqlAlchemyStore):
        store.engine.dispose()

    replacement_sql, expected_reason = _SCHEMA_DRIFT_CASES[drift]
    connection = connect_auth_database(db_path)
    try:
        object_type = "INDEX" if drift.startswith("partial_") else "TRIGGER"
        object_name = (
            "uq_account_invitations_pending_username"
            if object_type == "INDEX"
            else "trg_accounts_username_identity_immutable"
        )
        connection.execute(f"DROP {object_type} {object_name}")
        connection.execute(replacement_sql)
        changed_fingerprint = _schema_contract_fingerprint(connection)
    finally:
        connection.close()

    assert changed_fingerprint != baseline_fingerprint
    with pytest.raises(StorageMigrationRequiredError) as raised:
        if backend == "sqlite_raw":
            SQLiteStore(db_path)
        else:
            SqlAlchemyStore(db_path)
    assert raised.value.metadata["reason"] == expected_reason
