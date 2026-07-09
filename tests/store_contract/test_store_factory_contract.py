from __future__ import annotations

from pathlib import Path

from app.config import Settings
from app.db.sqlalchemy_store import SqlAlchemyStore
from app.db.sqlite_store import SQLiteStore
from app.db.store_factory import create_store


def test_store_factory_selects_backend(tmp_path: Path):
    raw = create_store(db_path=tmp_path / "raw.db", backend="sqlite_raw")
    sqlalchemy = create_store(db_path=tmp_path / "sqlalchemy.db", backend="sqlalchemy")

    assert isinstance(raw, SQLiteStore)
    assert isinstance(sqlalchemy, SqlAlchemyStore)


def test_settings_db_backend_defaults_to_sqlite_raw(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AEMEATH_DB_BACKEND", raising=False)
    monkeypatch.setattr("app.config.load_dotenv", lambda: None)

    settings = Settings.from_env()

    assert settings.db_backend == "sqlite_raw"
