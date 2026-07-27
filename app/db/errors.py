from __future__ import annotations

from app.errors.base import AemeathError
from app.errors.codes import ErrorCode


class StorageMigrationRequiredError(AemeathError):
    """Signals a database state that must be repaired explicitly."""

    def __init__(self, *, reason: str = "sqlalchemy_experimental_schema_incompatible"):
        super().__init__(
            code=ErrorCode.STORAGE_MIGRATION_REQUIRED,
            public_message="The database schema requires an explicit migration or rebuild.",
            internal_message=f"storage_migration_required:{reason}",
            metadata={"reason": reason},
        )
