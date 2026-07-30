from __future__ import annotations

import sqlite3
from pathlib import Path
from time import monotonic

from app.db.readiness import SqliteDatabaseReadinessAdapter
from app.db.sqlite_store import SQLiteStore


def test_sqlite_readiness_uses_read_only_schema_check(tmp_path: Path):
    db_path = tmp_path / "ready.db"
    SQLiteStore(db_path)
    adapter = SqliteDatabaseReadinessAdapter(db_path, timeout_seconds=0.2)

    assert adapter.check_readable(monotonic() + 0.5) is True

    with sqlite3.connect(db_path) as connection:
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 0


def test_sqlite_readiness_rejects_missing_schema_and_expired_deadline(tmp_path: Path):
    db_path = tmp_path / "empty.db"
    with sqlite3.connect(db_path):
        pass
    adapter = SqliteDatabaseReadinessAdapter(db_path, timeout_seconds=0.2)

    assert adapter.check_readable(monotonic() + 0.5) is False
    assert adapter.check_readable(monotonic() - 0.1) is False
