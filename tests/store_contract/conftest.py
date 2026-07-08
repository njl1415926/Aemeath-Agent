from __future__ import annotations

from pathlib import Path

import pytest

from app.db.sqlalchemy_store import SqlAlchemyStore
from app.db.sqlite_store import SQLiteStore


@pytest.fixture(params=["sqlite_raw", "sqlalchemy"])
def store(request: pytest.FixtureRequest, tmp_path: Path):
    backend = str(request.param)
    if backend == "sqlite_raw":
        return SQLiteStore(tmp_path / "raw.db")
    if backend == "sqlalchemy":
        return SqlAlchemyStore(tmp_path / "sqlalchemy.db")
    raise AssertionError(f"unknown store backend {backend!r}")
