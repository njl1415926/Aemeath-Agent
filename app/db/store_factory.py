from __future__ import annotations

from pathlib import Path
from typing import Literal, TypeAlias

from app.db.sqlalchemy_store import SqlAlchemyStore
from app.db.sqlite_store import SQLiteStore

DatabaseBackend = Literal["sqlite_raw", "sqlalchemy"]
StoreFacade: TypeAlias = SQLiteStore | SqlAlchemyStore


def create_store(*, db_path: Path, backend: str = "sqlite_raw") -> StoreFacade:
    if backend == "sqlite_raw":
        return SQLiteStore(db_path)
    if backend == "sqlalchemy":
        return SqlAlchemyStore(db_path)
    raise ValueError(
        "unsupported AEMEATH_DB_BACKEND; expected 'sqlite_raw' or 'sqlalchemy'"
    )
