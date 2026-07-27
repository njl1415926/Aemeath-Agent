from __future__ import annotations


class RagGenerationStoreError(RuntimeError):
    """A safe, stable failure from generation-store persistence operations."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class RagGenerationStoreReadOnlyError(RagGenerationStoreError):
    def __init__(self):
        super().__init__("rag_generation_store_read_only")


class RagGenerationStoreOpenError(RagGenerationStoreError):
    pass


class RagGenerationStoreBackupError(RagGenerationStoreError):
    pass


class RagIndexError(RuntimeError):
    """A content-safe managed-index failure with a stable reason code."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class RagIndexConfigError(RagIndexError):
    pass


class RagIndexManifestError(RagIndexError):
    pass


class RagIndexLockError(RagIndexError):
    pass


class RagIndexBuildError(RagIndexError):
    pass
