from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.db.errors import StorageMigrationRequiredError
from app.db.sqlalchemy_store import SqlAlchemyStore
from app.db.sqlite_store import SQLiteStore
from app.db.usage_quota_migrations import (
    USAGE_QUOTA_MIGRATION_ID,
    validate_usage_quota_schema_readonly,
)


@pytest.mark.parametrize("store_class", [SQLiteStore, SqlAlchemyStore])
def test_usage_quota_migration_is_idempotent_and_marked(tmp_path: Path, store_class):
    db_path = tmp_path / f"{store_class.__name__}.db"
    store_class(db_path)
    store_class(db_path)

    with sqlite3.connect(db_path) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM schema_migrations WHERE version = ?",
                (USAGE_QUOTA_MIGRATION_ID,),
            ).fetchone()[0]
            == 1
        )
        connection.row_factory = sqlite3.Row
        validate_usage_quota_schema_readonly(connection)


@pytest.mark.parametrize("store_class", [SQLiteStore, SqlAlchemyStore])
def test_usage_quota_marked_missing_table_fails_closed(tmp_path: Path, store_class):
    db_path = tmp_path / f"{store_class.__name__}.db"
    store_class(db_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute("DROP TABLE usage_quota_reservations")

    with pytest.raises(StorageMigrationRequiredError) as raised:
        store_class(db_path)

    assert raised.value.metadata["reason"] == "usage_quota_table_missing"


@pytest.mark.parametrize("store_class", [SQLiteStore, SqlAlchemyStore])
def test_usage_quota_marked_broken_index_fails_closed(tmp_path: Path, store_class):
    db_path = tmp_path / f"{store_class.__name__}.db"
    store_class(db_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute("DROP INDEX idx_usage_quota_reservations_runtime_phase")

    with pytest.raises(StorageMigrationRequiredError) as raised:
        store_class(db_path)

    assert raised.value.metadata["reason"] == "usage_quota_reservation_index_invalid"


def test_usage_quota_readonly_validation_requires_marker(tmp_path: Path):
    db_path = tmp_path / "marker.db"
    SQLiteStore(db_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "DELETE FROM schema_migrations WHERE version = ?",
            (USAGE_QUOTA_MIGRATION_ID,),
        )
        connection.row_factory = sqlite3.Row
        with pytest.raises(StorageMigrationRequiredError) as raised:
            validate_usage_quota_schema_readonly(connection)

    assert raised.value.metadata["reason"] == "usage_quota_marker_missing"
