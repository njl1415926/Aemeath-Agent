from __future__ import annotations

from collections import Counter

from app.embeddings.artifacts import (
    EmbeddingArtifactWrite,
    canonicalize_embedding_text,
    embedding_content_hash,
)
from app.embeddings.errors import EmbeddingInvalidResponseError
from app.embeddings.ports import EmbeddingProviderPort
from app.embeddings.schemas import EmbeddingRequest
from app.errors import map_exception
from app.memory.observability import record_memory_event
from app.memory.schemas import (
    MemoryEmbeddingArtifactGapCursor,
    MemoryEmbeddingRepairResult,
)
from app.memory.store import MemoryStore
from app.observability.noop import NoopAppLogger
from app.observability.ports import AppLoggerPort


class MemoryEmbeddingRepairService:
    def __init__(
        self,
        *,
        store: MemoryStore,
        embedding_provider: EmbeddingProviderPort,
        app_logger: AppLoggerPort | None = None,
    ):
        self.store = store
        self.embedding_provider = embedding_provider
        self.app_logger = app_logger or NoopAppLogger()

    def repair_batch(
        self,
        *,
        cursor: MemoryEmbeddingArtifactGapCursor | None = None,
        limit: int,
        user_id: str | None = None,
    ) -> MemoryEmbeddingRepairResult:
        if limit <= 0:
            raise ValueError("memory repair limit must be positive")
        page = self.store.list_memory_embedding_artifact_gaps(
            query_signature=self.embedding_provider.signature,
            user_id=user_id,
            cursor=cursor,
            limit=limit,
        )
        reason_counts = dict(Counter(gap.reason for gap in page.gaps))
        if not page.gaps:
            result = MemoryEmbeddingRepairResult(
                scanned_memory_count=page.scanned_memory_count,
                gap_count=0,
                artifact_written_memory_ids=[],
                embedding_pending_memory_ids=[],
                next_cursor=page.next_cursor,
            )
            self._record(result=result, reason_counts=reason_counts, user_id=user_id)
            return result

        canonical_contents = [
            canonicalize_embedding_text(gap.content) for gap in page.gaps
        ]
        try:
            batch = self.embedding_provider.embed(
                EmbeddingRequest(texts=tuple(canonical_contents), purpose="document")
            )
            if batch.signature.canonical() != self.embedding_provider.signature.canonical():
                raise EmbeddingInvalidResponseError("repair_signature_mismatch")
            if batch.vectors.shape[0] != len(page.gaps):
                raise EmbeddingInvalidResponseError("repair_result_count_mismatch")
        except Exception as exc:
            result = MemoryEmbeddingRepairResult(
                scanned_memory_count=page.scanned_memory_count,
                gap_count=len(page.gaps),
                artifact_written_memory_ids=[],
                embedding_pending_memory_ids=[gap.memory_id for gap in page.gaps],
                next_cursor=cursor,
                error_code=map_exception(exc).code.value,
            )
            self._record(result=result, reason_counts=reason_counts, user_id=user_id)
            return result

        written_ids: list[str] = []
        pending_ids: list[str] = []
        error_code: str | None = None
        for gap, canonical_content, vector in zip(
            page.gaps,
            canonical_contents,
            batch.vectors,
            strict=True,
        ):
            try:
                outcome = self.store.upsert_memory_embedding_artifact(
                    memory_id=gap.memory_id,
                    user_id=gap.user_id,
                    artifact=EmbeddingArtifactWrite(
                        signature=batch.signature,
                        content_hash=embedding_content_hash(canonical_content),
                        vector=vector,
                    ),
                )
                if outcome.status in {"created", "replaced"}:
                    written_ids.append(gap.memory_id)
            except Exception as exc:
                pending_ids.append(gap.memory_id)
                if error_code is None:
                    error_code = map_exception(exc).code.value

        result = MemoryEmbeddingRepairResult(
            scanned_memory_count=page.scanned_memory_count,
            gap_count=len(page.gaps),
            artifact_written_memory_ids=written_ids,
            embedding_pending_memory_ids=pending_ids,
            next_cursor=cursor if pending_ids else page.next_cursor,
            error_code=error_code,
        )
        self._record(result=result, reason_counts=reason_counts, user_id=user_id)
        return result

    def _record(
        self,
        *,
        result: MemoryEmbeddingRepairResult,
        reason_counts: dict[str, int],
        user_id: str | None,
    ) -> None:
        record_memory_event(
            self.app_logger,
            event_name="memory.repair_completed",
            level="warning" if result.embedding_pending_memory_ids else "info",
            error_code=result.error_code,
            payload={
                "provider": self.embedding_provider.signature.provider,
                "profile_id": self.embedding_provider.signature.profile_id,
                "scanned_memory_count": result.scanned_memory_count,
                "gap_count": result.gap_count,
                "gap_reason_counts": reason_counts,
                "artifact_written_count": len(result.artifact_written_memory_ids),
                "embedding_pending_count": len(result.embedding_pending_memory_ids),
            },
            user_id=user_id,
        )
