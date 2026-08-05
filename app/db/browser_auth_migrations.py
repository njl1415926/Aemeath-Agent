from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path

from app.auth.schemas import StoreSchemaInfo
from app.db.errors import StorageMigrationRequiredError
from app.utils.ids import utc_now

BROWSER_AUTH_MIGRATION_ID = "browser_auth_v0_1"
BROWSER_AUTH_SCHEMA_REVISION = "browser_auth_v0_1"

_TRIGGERS = {
    "trg_accounts_username_normalized_insert_nonempty": """
        CREATE TRIGGER trg_accounts_username_normalized_insert_nonempty
        BEFORE INSERT ON accounts
        WHEN NEW.username_normalized = ''
        BEGIN
            SELECT RAISE(ABORT, 'account_username_normalized_empty');
        END
    """,
    "trg_accounts_username_normalized_update_nonempty": """
        CREATE TRIGGER trg_accounts_username_normalized_update_nonempty
        BEFORE UPDATE OF username_normalized ON accounts
        WHEN NEW.username_normalized = ''
        BEGIN
            SELECT RAISE(ABORT, 'account_username_normalized_empty');
        END
    """,
    "trg_accounts_username_identity_immutable": """
        CREATE TRIGGER trg_accounts_username_identity_immutable
        BEFORE UPDATE OF username, username_normalized ON accounts
        WHEN NEW.username <> OLD.username
          OR NEW.username_normalized <> OLD.username_normalized
        BEGIN
            SELECT RAISE(ABORT, 'account_username_immutable');
        END
    """,
}

_AUTH_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS account_credentials (
    account_id TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    credential_version INTEGER NOT NULL CHECK (credential_version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    password_changed_at TEXT NOT NULL,
    FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS account_invitations (
    id TEXT PRIMARY KEY,
    token_digest TEXT NOT NULL UNIQUE,
    username TEXT NOT NULL,
    username_normalized TEXT NOT NULL,
    display_name TEXT,
    roles_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'used', 'revoked')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT,
    revoked_at TEXT,
    accepted_account_id TEXT,
    FOREIGN KEY (accepted_account_id) REFERENCES accounts(id) ON DELETE RESTRICT,
    CHECK (
      (status = 'pending' AND used_at IS NULL AND revoked_at IS NULL
          AND accepted_account_id IS NULL) OR
      (status = 'used' AND used_at IS NOT NULL AND revoked_at IS NULL
          AND accepted_account_id IS NOT NULL) OR
      (status = 'revoked' AND used_at IS NULL AND revoked_at IS NOT NULL
          AND accepted_account_id IS NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_account_invitations_pending_username
    ON account_invitations (username_normalized) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_account_invitations_status_expiry
    ON account_invitations (status, expires_at, created_at, id);
CREATE INDEX IF NOT EXISTS idx_account_invitations_username_status
    ON account_invitations (username_normalized, status);

CREATE TABLE IF NOT EXISTS account_password_resets (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    token_digest TEXT NOT NULL UNIQUE,
    issued_credential_version INTEGER NOT NULL CHECK (issued_credential_version >= 0),
    status TEXT NOT NULL CHECK (status IN ('pending', 'used', 'revoked')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT,
    revoked_at TEXT,
    FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE RESTRICT,
    CHECK (
      (status = 'pending' AND used_at IS NULL AND revoked_at IS NULL) OR
      (status = 'used' AND used_at IS NOT NULL AND revoked_at IS NULL) OR
      (status = 'revoked' AND used_at IS NULL AND revoked_at IS NOT NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_account_password_resets_pending_account
    ON account_password_resets (account_id) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_account_password_resets_status_expiry
    ON account_password_resets (status, expires_at, created_at, id);
CREATE INDEX IF NOT EXISTS idx_account_password_resets_account_status
    ON account_password_resets (account_id, status);

CREATE TABLE IF NOT EXISTS web_auth_sessions (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    token_digest TEXT NOT NULL UNIQUE,
    csrf_verifier BLOB NOT NULL,
    authenticated_credential_version INTEGER NOT NULL
        CHECK (authenticated_credential_version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    idle_expires_at TEXT NOT NULL,
    absolute_expires_at TEXT NOT NULL,
    revoked_at TEXT,
    revoke_reason TEXT,
    FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE RESTRICT,
    CHECK (length(csrf_verifier) = 32),
    CHECK ((revoked_at IS NULL AND revoke_reason IS NULL) OR
           (revoked_at IS NOT NULL AND revoke_reason IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS idx_web_auth_sessions_account_active
    ON web_auth_sessions (account_id, revoked_at, absolute_expires_at);
CREATE INDEX IF NOT EXISTS idx_web_auth_sessions_absolute_expiry
    ON web_auth_sessions (absolute_expires_at, id);
CREATE INDEX IF NOT EXISTS idx_web_auth_sessions_revoked
    ON web_auth_sessions (revoked_at, id);
"""

_REQUIRED_TABLE_COLUMNS = {
    "account_credentials": {
        "account_id",
        "password_hash",
        "credential_version",
        "created_at",
        "updated_at",
        "password_changed_at",
    },
    "account_invitations": {
        "id",
        "token_digest",
        "username",
        "username_normalized",
        "display_name",
        "roles_json",
        "status",
        "created_at",
        "updated_at",
        "expires_at",
        "used_at",
        "revoked_at",
        "accepted_account_id",
    },
    "account_password_resets": {
        "id",
        "account_id",
        "token_digest",
        "issued_credential_version",
        "status",
        "created_at",
        "updated_at",
        "expires_at",
        "used_at",
        "revoked_at",
    },
    "web_auth_sessions": {
        "id",
        "account_id",
        "token_digest",
        "csrf_verifier",
        "authenticated_credential_version",
        "created_at",
        "updated_at",
        "last_seen_at",
        "idle_expires_at",
        "absolute_expires_at",
        "revoked_at",
        "revoke_reason",
    },
}

_REQUIRED_INDEXES = {
    "uq_account_invitations_pending_username": (
        "account_invitations",
        True,
        ("username_normalized",),
        True,
        "status = 'pending'",
    ),
    "idx_account_invitations_status_expiry": (
        "account_invitations",
        False,
        ("status", "expires_at", "created_at", "id"),
        False,
        None,
    ),
    "idx_account_invitations_username_status": (
        "account_invitations",
        False,
        ("username_normalized", "status"),
        False,
        None,
    ),
    "uq_account_password_resets_pending_account": (
        "account_password_resets",
        True,
        ("account_id",),
        True,
        "status = 'pending'",
    ),
    "idx_account_password_resets_status_expiry": (
        "account_password_resets",
        False,
        ("status", "expires_at", "created_at", "id"),
        False,
        None,
    ),
    "idx_account_password_resets_account_status": (
        "account_password_resets",
        False,
        ("account_id", "status"),
        False,
        None,
    ),
    "idx_web_auth_sessions_account_active": (
        "web_auth_sessions",
        False,
        ("account_id", "revoked_at", "absolute_expires_at"),
        False,
        None,
    ),
    "idx_web_auth_sessions_absolute_expiry": (
        "web_auth_sessions",
        False,
        ("absolute_expires_at", "id"),
        False,
        None,
    ),
    "idx_web_auth_sessions_revoked": (
        "web_auth_sessions",
        False,
        ("revoked_at", "id"),
        False,
        None,
    ),
}

_REQUIRED_FOREIGN_KEYS = {
    "account_credentials": ("account_id", "accounts", "id", "RESTRICT"),
    "account_invitations": ("accepted_account_id", "accounts", "id", "RESTRICT"),
    "account_password_resets": ("account_id", "accounts", "id", "RESTRICT"),
    "web_auth_sessions": ("account_id", "accounts", "id", "RESTRICT"),
}

_NULLABLE_AUTH_COLUMNS = {
    "account_invitations": {
        "display_name",
        "used_at",
        "revoked_at",
        "accepted_account_id",
    },
    "account_password_resets": {"used_at", "revoked_at"},
    "web_auth_sessions": {"revoked_at", "revoke_reason"},
}

_AUTH_PRIMARY_KEYS = {
    "account_credentials": "account_id",
    "account_invitations": "id",
    "account_password_resets": "id",
    "web_auth_sessions": "id",
}

_INTEGER_AUTH_COLUMNS = {
    "authenticated_credential_version",
    "credential_version",
    "issued_credential_version",
}
_BLOB_AUTH_COLUMNS = {"csrf_verifier"}


def connect_auth_database(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path, isolation_level=None, timeout=5.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        connection.close()
        raise StorageMigrationRequiredError(reason="foreign_keys_not_enabled")
    return connection


def ensure_browser_auth_schema(db_path: Path) -> None:
    with closing(connect_auth_database(db_path)) as connection:
        migration_table_exists = connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = 'schema_migrations'"
        ).fetchone()
        applied = None
        if migration_table_exists is not None:
            applied = connection.execute(
                "SELECT 1 FROM schema_migrations WHERE version = ?",
                (BROWSER_AUTH_MIGRATION_ID,),
            ).fetchone()
        if applied is not None:
            _validate_schema(connection)
            _require_foreign_keys_clean(connection)
            return

        accounts = connection.execute("SELECT id, username FROM accounts ORDER BY id").fetchall()
        normalized_by_id = {row["id"]: row["username"].lower() for row in accounts}
        if len(set(normalized_by_id.values())) != len(normalized_by_id):
            raise StorageMigrationRequiredError(reason="account_username_normalized_collision")

        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            columns = _table_columns(connection, "accounts")
            if "username_normalized" not in columns:
                connection.execute(
                    "ALTER TABLE accounts ADD COLUMN username_normalized TEXT NOT NULL DEFAULT ''"
                )
            for account_id, normalized in normalized_by_id.items():
                connection.execute(
                    "UPDATE accounts SET username_normalized = ? WHERE id = ?",
                    (normalized, account_id),
                )
            sentinel_count = connection.execute(
                "SELECT COUNT(*) FROM accounts WHERE username_normalized = ''"
            ).fetchone()[0]
            if sentinel_count:
                raise StorageMigrationRequiredError(reason="account_username_normalized_sentinel")
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_accounts_username_normalized "
                "ON accounts (username_normalized)"
            )
            for name, sql in _TRIGGERS.items():
                existing = connection.execute(
                    "SELECT sql FROM sqlite_schema WHERE type = 'trigger' AND name = ?",
                    (name,),
                ).fetchone()
                if existing is None:
                    connection.execute(sql)
            _execute_schema_script(connection, _AUTH_SCHEMA_SQL)
            _validate_schema(connection)
            _require_foreign_keys_clean(connection)
            connection.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (BROWSER_AUTH_MIGRATION_ID, utc_now()),
            )
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        _validate_schema(connection)
        _require_foreign_keys_clean(connection)


def store_schema_info(db_path: Path, *, backend: str) -> StoreSchemaInfo:
    with closing(connect_auth_database(db_path)) as connection:
        _validate_schema(connection)
        migration_ids = tuple(
            row["version"]
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        )
        fingerprint = _schema_contract_fingerprint(connection)
    return StoreSchemaInfo(
        db_backend=backend,
        migration_ids=migration_ids,
        browser_auth_schema_revision=BROWSER_AUTH_SCHEMA_REVISION,
        schema_contract_fingerprint=fingerprint,
    )


def _validate_schema(connection: sqlite3.Connection) -> None:
    accounts = _table_columns(connection, "accounts")
    column = accounts.get("username_normalized")
    if (
        column is None
        or str(column["type"]).upper() != "TEXT"
        or int(column["notnull"]) != 1
        or column["dflt_value"] != "''"
    ):
        raise StorageMigrationRequiredError(reason="account_username_normalized_invalid")
    if connection.execute(
        "SELECT COUNT(*) FROM accounts WHERE username_normalized = ''"
    ).fetchone()[0]:
        raise StorageMigrationRequiredError(reason="account_username_normalized_sentinel")

    indexes = {
        row["name"]: row for row in connection.execute("PRAGMA index_list('accounts')").fetchall()
    }
    username_index = indexes.get("uq_accounts_username_normalized")
    if username_index is None or int(username_index["unique"]) != 1:
        raise StorageMigrationRequiredError(reason="account_username_index_invalid")
    key_columns = [
        row["name"]
        for row in connection.execute(
            "PRAGMA index_xinfo('uq_accounts_username_normalized')"
        ).fetchall()
        if int(row["key"]) == 1
    ]
    if key_columns != ["username_normalized"]:
        raise StorageMigrationRequiredError(reason="account_username_index_invalid")
    username_index_sql = connection.execute(
        "SELECT sql FROM sqlite_schema WHERE type = 'index' "
        "AND name = 'uq_accounts_username_normalized'"
    ).fetchone()
    if _normalize_sql(username_index_sql["sql"] if username_index_sql else "") != _normalize_sql(
        "CREATE UNIQUE INDEX uq_accounts_username_normalized "
        "ON accounts (username_normalized)"
    ):
        raise StorageMigrationRequiredError(reason="account_username_index_invalid")

    for name in _TRIGGERS:
        row = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE type = 'trigger' AND name = ?",
            (name,),
        ).fetchone()
        if row is None or not _trigger_has_contract(name, row["sql"] or ""):
            raise StorageMigrationRequiredError(reason="account_username_trigger_invalid")

    _validate_auth_columns(connection)
    _validate_auth_table_guards(connection)
    _validate_auth_indexes(connection)
    _validate_auth_foreign_keys(connection)


def _validate_auth_table_guards(connection: sqlite3.Connection) -> None:
    expected_checks = _expected_auth_check_expressions()
    for table_name in _REQUIRED_TABLE_COLUMNS:
        row = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()
        sql = row["sql"] if row else ""
        if _extract_check_expressions(sql) != expected_checks[table_name]:
            raise StorageMigrationRequiredError(reason="browser_auth_check_invalid")
        if table_name != "account_credentials" and not _has_exact_unique_index(
            connection,
            table_name=table_name,
            columns=("token_digest",),
            partial=False,
        ):
            raise StorageMigrationRequiredError(reason="browser_auth_token_unique_invalid")


def _validate_auth_indexes(connection: sqlite3.Connection) -> None:
    for name, (table_name, unique, columns, partial, predicate) in _REQUIRED_INDEXES.items():
        rows = {
            row["name"]: row
            for row in connection.execute(f"PRAGMA index_list('{table_name}')").fetchall()
        }
        row = rows.get(name)
        if row is None or bool(row["unique"]) is not unique or bool(row["partial"]) is not partial:
            raise StorageMigrationRequiredError(reason="browser_auth_index_invalid")
        key_columns = tuple(
            item["name"]
            for item in connection.execute(f"PRAGMA index_xinfo('{name}')").fetchall()
            if int(item["key"]) == 1
        )
        if key_columns != columns:
            raise StorageMigrationRequiredError(reason="browser_auth_index_invalid")
        sql_row = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE type = 'index' AND name = ?",
            (name,),
        ).fetchone()
        expected_sql = (
            "CREATE "
            + ("UNIQUE " if unique else "")
            + f"INDEX {name} ON {table_name} ({', '.join(columns)})"
            + (f" WHERE {predicate}" if predicate is not None else "")
        )
        if _normalize_sql(sql_row["sql"] if sql_row else "") != _normalize_sql(expected_sql):
            raise StorageMigrationRequiredError(reason="browser_auth_index_invalid")


def _validate_auth_foreign_keys(connection: sqlite3.Connection) -> None:
    for table_name, expected in _REQUIRED_FOREIGN_KEYS.items():
        rows = connection.execute(f"PRAGMA foreign_key_list('{table_name}')").fetchall()
        contracts = {
            (row["from"], row["table"], row["to"], row["on_delete"].upper()) for row in rows
        }
        if contracts != {expected}:
            raise StorageMigrationRequiredError(reason="browser_auth_foreign_key_invalid")


def _trigger_has_contract(name: str, sql: str) -> bool:
    expected = _TRIGGERS.get(name)
    return expected is not None and _normalize_sql(sql) == _normalize_sql(expected)


def _table_columns(connection: sqlite3.Connection, table_name: str) -> dict[str, sqlite3.Row]:
    return {
        row["name"]: row
        for row in connection.execute(f"PRAGMA table_xinfo('{table_name}')").fetchall()
    }


def _validate_auth_columns(connection: sqlite3.Connection) -> None:
    for table_name, expected_names in _REQUIRED_TABLE_COLUMNS.items():
        columns = _table_columns(connection, table_name)
        if set(columns) != expected_names:
            raise StorageMigrationRequiredError(reason="browser_auth_columns_invalid")

        primary_key = _AUTH_PRIMARY_KEYS[table_name]
        nullable = _NULLABLE_AUTH_COLUMNS.get(table_name, set())
        for name, column in columns.items():
            expected_type = (
                "INTEGER"
                if name in _INTEGER_AUTH_COLUMNS
                else "BLOB"
                if name in _BLOB_AUTH_COLUMNS
                else "TEXT"
            )
            if str(column["type"]).upper() != expected_type:
                raise StorageMigrationRequiredError(reason="browser_auth_columns_invalid")
            if column["dflt_value"] is not None or int(column["hidden"]) != 0:
                raise StorageMigrationRequiredError(reason="browser_auth_columns_invalid")
            if (name == primary_key) is not bool(column["pk"]):
                raise StorageMigrationRequiredError(reason="browser_auth_columns_invalid")
            # SQLite reports TEXT PRIMARY KEY as nullable for raw DDL but SQLAlchemy
            # emits an explicit NOT NULL. The primary-key invariant is authoritative.
            if name != primary_key and bool(column["notnull"]) is (name in nullable):
                raise StorageMigrationRequiredError(reason="browser_auth_columns_invalid")


def _has_exact_unique_index(
    connection: sqlite3.Connection,
    *,
    table_name: str,
    columns: tuple[str, ...],
    partial: bool,
) -> bool:
    for row in connection.execute(f"PRAGMA index_list('{table_name}')").fetchall():
        if not bool(row["unique"]) or bool(row["partial"]) is not partial:
            continue
        key_columns = tuple(
            item["name"]
            for item in connection.execute(
                f"PRAGMA index_xinfo('{row['name']}')"
            ).fetchall()
            if int(item["key"]) == 1
        )
        if key_columns == columns:
            return True
    return False


def _expected_auth_check_expressions() -> dict[str, tuple[str, ...]]:
    expected: dict[str, tuple[str, ...]] = {}
    for statement in _AUTH_SCHEMA_SQL.split(";"):
        tokens = _sql_tokens(statement)
        if len(tokens) < 3 or tokens[:2] != ("create", "table"):
            continue
        name_index = 2
        if tokens[name_index : name_index + 3] == ("if", "not", "exists"):
            name_index += 3
        expected[tokens[name_index]] = _extract_check_expressions(statement)
    return expected


def _extract_check_expressions(sql: str) -> tuple[str, ...]:
    tokens = _sql_tokens(sql)
    expressions: list[str] = []
    index = 0
    while index + 1 < len(tokens):
        if tokens[index] != "check" or tokens[index + 1] != "(":
            index += 1
            continue
        depth = 1
        cursor = index + 2
        expression: list[str] = []
        while cursor < len(tokens) and depth:
            token = tokens[cursor]
            if token == "(":
                depth += 1
            elif token == ")":
                depth -= 1
                if depth == 0:
                    break
            expression.append(token)
            cursor += 1
        if depth:
            return ()
        expressions.append("".join(expression))
        index = cursor + 1
    return tuple(sorted(expressions))


def _schema_contract_fingerprint(connection: sqlite3.Connection) -> str:
    names = sorted(
        {
            "accounts",
            "uq_accounts_username_normalized",
            *_TRIGGERS,
            *_REQUIRED_TABLE_COLUMNS,
            *_REQUIRED_INDEXES,
        }
    )
    placeholders = ", ".join("?" for _ in names)
    rows = connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_schema "
        f"WHERE name IN ({placeholders}) ORDER BY type, name",
        names,
    ).fetchall()
    payload = "\n".join(
        "|".join(
            (
                str(row["type"]).lower(),
                str(row["name"]).lower(),
                str(row["tbl_name"]).lower(),
                _normalize_sql(row["sql"] or ""),
            )
        )
        for row in rows
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _require_foreign_keys_clean(connection: sqlite3.Connection) -> None:
    rows = connection.execute("PRAGMA foreign_key_check").fetchall()
    if rows:
        raise StorageMigrationRequiredError(reason="foreign_key_violation")


def _normalize_sql(sql: str) -> str:
    tokens = list(_sql_tokens(sql))
    while tokens and tokens[-1] == ";":
        tokens.pop()
    return "".join(tokens)


def _sql_tokens(sql: str) -> tuple[str, ...]:
    """Tokenize SQLite DDL while preserving single-quoted literal bytes."""

    tokens: list[str] = []
    index = 0
    paired_operators = {"!=", "<=", "<>", "==", ">=", "||"}
    punctuation = set("(),;=<>!+-*/%|")
    while index < len(sql):
        character = sql[index]
        if character.isspace():
            index += 1
            continue
        if character == "'":
            start = index
            index += 1
            while index < len(sql):
                if sql[index] != "'":
                    index += 1
                    continue
                index += 1
                if index < len(sql) and sql[index] == "'":
                    index += 1
                    continue
                break
            tokens.append(sql[start:index])
            continue
        if character in {'"', "`", "["}:
            closing_character = "]" if character == "[" else character
            start = index
            index += 1
            while index < len(sql):
                if sql[index] != closing_character:
                    index += 1
                    continue
                index += 1
                if (
                    closing_character != "]"
                    and index < len(sql)
                    and sql[index] == closing_character
                ):
                    index += 1
                    continue
                break
            tokens.append(sql[start:index].lower())
            continue
        pair = sql[index : index + 2]
        if pair in paired_operators:
            tokens.append(pair)
            index += 2
            continue
        if character in punctuation:
            tokens.append(character)
            index += 1
            continue
        start = index
        while index < len(sql):
            if (
                sql[index].isspace()
                or sql[index] in punctuation
                or sql[index] in {'"', "'", "`", "["}
            ):
                break
            index += 1
        tokens.append(sql[start:index].lower())
    return tuple(tokens)


def _execute_schema_script(connection: sqlite3.Connection, script: str) -> None:
    for statement in script.split(";"):
        sql = statement.strip()
        if sql:
            connection.execute(sql)
