from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine, create_engine, inspect
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from app.db.errors import StorageMigrationRequiredError
from app.db.models import Base
from app.db.sqlite_index_contract import (
    ACTIVE_MEMORY_IDENTITY_INDEX,
    has_valid_active_memory_identity_index,
)

_MEMORY_ARTIFACT_TABLE = "long_term_memory_embedding_artifacts"
_MEMORY_ARTIFACT_REQUIRED_COLUMNS = {
    "memory_id",
    "user_id",
    "embedding_signature_json",
    "embedding_signature_fingerprint",
    "embedding_purpose",
    "embedding_content_hash",
    "embedding_vector_format",
    "vector_dim",
    "embedding",
    "embedding_vector_sha256",
    "created_at",
    "updated_at",
}
_MEMORY_ARTIFACT_PRIMARY_KEY = {
    "memory_id",
    "embedding_signature_fingerprint",
    "embedding_purpose",
}
def _has_valid_active_memory_identity_index(engine: Engine) -> bool:
    with engine.connect() as connection:
        index_list_rows = connection.exec_driver_sql(
            "PRAGMA index_list('long_term_memories')"
        ).mappings().all()
        index_xinfo_rows = connection.exec_driver_sql(
            f"PRAGMA index_xinfo('{ACTIVE_MEMORY_IDENTITY_INDEX}')"
        ).mappings().all()
        sql = connection.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
            (ACTIVE_MEMORY_IDENTITY_INDEX,),
        ).scalar_one_or_none()
    return has_valid_active_memory_identity_index(
        index_list_rows=index_list_rows,
        index_xinfo_rows=index_xinfo_rows,
        index_sql=sql,
    )


def create_sqlite_engine(db_path: Path) -> Engine:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return create_engine(
        f"sqlite:///{db_path.as_posix()}",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=NullPool,
    )


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


def initialize_database(engine: Engine) -> None:
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    if (
        "long_term_memory_embeddings" in table_names
        and _MEMORY_ARTIFACT_TABLE not in table_names
    ):
        raise StorageMigrationRequiredError(
            reason="sqlalchemy_embedding_artifact_table_missing"
        )
    if _MEMORY_ARTIFACT_TABLE in table_names:
        columns = {
            column["name"]
            for column in inspector.get_columns(_MEMORY_ARTIFACT_TABLE)
        }
        primary_key = set(
            inspector.get_pk_constraint(_MEMORY_ARTIFACT_TABLE).get(
                "constrained_columns", []
            )
        )
        checks = "\n".join(
            check.get("sqltext") or ""
            for check in inspector.get_check_constraints(_MEMORY_ARTIFACT_TABLE)
        ).replace(" ", "").lower()
        if (
            not _MEMORY_ARTIFACT_REQUIRED_COLUMNS.issubset(columns)
            or primary_key != _MEMORY_ARTIFACT_PRIMARY_KEY
            or "embedding_purpose='document'" not in checks
            or "embedding_vector_format='f32le-v1'" not in checks
        ):
            raise StorageMigrationRequiredError(
                reason="sqlalchemy_embedding_artifact_schema_invalid"
            )
    if (
        "long_term_memories" in table_names
        and not _has_valid_active_memory_identity_index(engine)
    ):
        raise StorageMigrationRequiredError(
            reason="sqlalchemy_active_memory_identity_index_invalid"
        )
    Base.metadata.create_all(engine)
    if not _has_valid_active_memory_identity_index(engine):
        raise StorageMigrationRequiredError(
            reason="sqlalchemy_active_memory_identity_index_invalid"
        )
