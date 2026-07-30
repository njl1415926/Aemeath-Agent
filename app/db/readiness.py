from __future__ import annotations

import sqlite3
from pathlib import Path
from time import monotonic

from app.runtime.readiness import DatabaseReadinessPort

_RAW_REQUIRED_TABLES = frozenset({"accounts", "schema_migrations", "sessions"})
_SQLALCHEMY_REQUIRED_TABLES = frozenset({"accounts", "messages", "sessions"})


class SqliteDatabaseReadinessAdapter(DatabaseReadinessPort):
    """Short-lived read-only SQLite probe hidden behind the readiness port."""

    def __init__(
        self,
        db_path: Path,
        *,
        timeout_seconds: float,
        required_tables: frozenset[str] = _RAW_REQUIRED_TABLES,
    ):
        self._db_path = db_path
        self._timeout_seconds = timeout_seconds
        self._required_tables = required_tables

    def check_readable(self, deadline: float) -> bool:
        remaining = min(self._timeout_seconds, deadline - monotonic())
        if remaining <= 0:
            return False
        connection: sqlite3.Connection | None = None
        try:
            uri = f"{self._db_path.resolve().as_uri()}?mode=ro"
            connection = sqlite3.connect(uri, uri=True, timeout=remaining)
            connection.execute("PRAGMA query_only = ON")
            rows = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
            table_names = {str(row[0]) for row in rows}
            return monotonic() < deadline and self._required_tables <= table_names
        except (OSError, sqlite3.Error, ValueError):
            return False
        finally:
            if connection is not None:
                connection.close()


def build_database_readiness(
    *,
    backend: str,
    db_path: Path,
    timeout_seconds: float,
) -> DatabaseReadinessPort:
    if backend not in {"sqlite_raw", "sqlalchemy"}:
        raise ValueError("No approved readiness adapter exists for this database backend.")
    # Both current backends persist to the configured SQLite file. Keep the
    # readiness boundary independent of either store implementation while
    # recognizing their intentionally different schema bootstrap markers.
    required_tables = (
        _RAW_REQUIRED_TABLES if backend == "sqlite_raw" else _SQLALCHEMY_REQUIRED_TABLES
    )
    return SqliteDatabaseReadinessAdapter(
        db_path,
        timeout_seconds=timeout_seconds,
        required_tables=required_tables,
    )
