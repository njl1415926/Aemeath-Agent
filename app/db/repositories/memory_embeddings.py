from __future__ import annotations

import numpy as np
from sqlalchemy import and_, or_
from sqlalchemy.orm import aliased

from app.db.memory_artifact_contract import (
    StoredMemoryEmbeddingArtifact,
    classify_memory_embedding_artifact_gap,
)
from app.db.models import (
    LongTermMemoryEmbeddingArtifactModel,
    LongTermMemoryEmbeddingModel,
    LongTermMemoryModel,
)
from app.db.repositories.common import SessionFactory
from app.embeddings.artifacts import (
    EmbeddingArtifactDiagnostics,
    EmbeddingArtifactUpsertOutcome,
    EmbeddingArtifactValidationError,
    EmbeddingArtifactWrite,
    canonical_content_hash,
    deterministic_hash_v1_legacy_signature,
    diagnostics_from_reasons,
    is_promoted_deterministic_hash_legacy,
    prepare_embedding_artifact,
    prepare_embedding_query,
    validate_stored_embedding_artifact,
)
from app.embeddings.schemas import EmbeddingModelSignature
from app.memory.schemas import (
    MemoryArtifactEligibility,
    MemoryEmbeddingArtifactGap,
    MemoryEmbeddingArtifactGapCursor,
    MemoryEmbeddingArtifactGapPage,
    MemoryEmbeddingSearchOutcome,
    MemoryVectorSearchResult,
)
from app.utils.ids import utc_now

_MAX_MEMORY_ARTIFACT_GAP_LIMIT = 100
_MAX_MEMORY_ARTIFACT_GAP_SCAN_LIMIT = 400


class MemoryEmbeddingRepository:
    def __init__(self, session_factory: SessionFactory):
        self._session_factory = session_factory

    def _scope_filter(self, *, character_id: str | None, session_id: str | None):
        return (
            (LongTermMemoryModel.scope == "user_global")
            | (
                (LongTermMemoryModel.scope == "character")
                & (LongTermMemoryModel.scope_character_id == character_id)
            )
            | (
                (LongTermMemoryModel.scope == "session")
                & (LongTermMemoryModel.scope_character_id == character_id)
                & (LongTermMemoryModel.scope_session_id == session_id)
            )
        )

    def _eligibility_filters(self, eligibility: MemoryArtifactEligibility):
        filters = [
            LongTermMemoryModel.user_id == eligibility.user_id,
            LongTermMemoryModel.status == "active",
            LongTermMemoryModel.is_active == 1,
            self._scope_filter(
                character_id=eligibility.character_id,
                session_id=eligibility.session_id,
            ),
        ]
        if eligibility.namespaces:
            filters.append(LongTermMemoryModel.namespace.in_(eligibility.namespaces))
        if eligibility.memory_types:
            filters.append(LongTermMemoryModel.memory_type.in_(eligibility.memory_types))
        if eligibility.min_confidence is not None:
            filters.append(LongTermMemoryModel.confidence >= eligibility.min_confidence)
        return tuple(filters)

    def upsert_memory_embedding(
        self,
        *,
        memory_id: str,
        user_id: str,
        embedding_model: str,
        embedding: np.ndarray,
    ) -> None:
        vector = np.asarray(embedding, dtype=np.float32)
        with self._session_factory() as session:
            model = session.get(LongTermMemoryEmbeddingModel, memory_id)
            if model is None:
                model = LongTermMemoryEmbeddingModel(
                    memory_id=memory_id,
                    user_id=user_id,
                    embedding_model=embedding_model,
                    vector_dim=int(vector.shape[0]),
                    embedding=vector.tobytes(),
                    created_at=utc_now(),
                    updated_at=None,
                )
                session.add(model)
            else:
                model.user_id = user_id
                model.embedding_model = embedding_model
                model.vector_dim = int(vector.shape[0])
                model.embedding = vector.tobytes()
                model.updated_at = utc_now()
            session.commit()

    def search_memory_embeddings(
        self,
        *,
        user_id: str,
        character_id: str | None,
        session_id: str | None,
        query_embedding: np.ndarray,
        limit: int,
        candidate_limit: int | None = None,
    ) -> list[MemoryVectorSearchResult]:
        candidate_limit = candidate_limit or max(limit * 4, limit + 8)
        query = np.asarray(query_embedding, dtype=np.float32)
        results: list[MemoryVectorSearchResult] = []
        with self._session_factory() as session:
            rows = (
                session.query(
                    LongTermMemoryEmbeddingModel.memory_id,
                    LongTermMemoryEmbeddingModel.embedding,
                )
                .join(
                    LongTermMemoryModel,
                    LongTermMemoryModel.id == LongTermMemoryEmbeddingModel.memory_id,
                )
                .filter(
                    LongTermMemoryEmbeddingModel.user_id == user_id,
                    LongTermMemoryModel.user_id == user_id,
                    LongTermMemoryModel.status == "active",
                    LongTermMemoryModel.is_active == 1,
                    self._scope_filter(
                        character_id=character_id,
                        session_id=session_id,
                    ),
                )
                .all()
            )
        for memory_id, raw_embedding in rows:
            vector = np.frombuffer(raw_embedding, dtype=np.float32)
            if vector.shape != query.shape:
                continue
            results.append(
                MemoryVectorSearchResult(
                    memory_id=memory_id,
                    score=float(np.dot(query, vector)),
                )
            )
        results.sort(key=lambda item: item.score, reverse=True)
        return results[:candidate_limit]

    def upsert_memory_embedding_artifact(
        self,
        *,
        memory_id: str,
        user_id: str,
        artifact: EmbeddingArtifactWrite,
    ) -> EmbeddingArtifactUpsertOutcome:
        prepared = prepare_embedding_artifact(artifact)
        with self._session_factory() as session:
            # SQLite needs a writer transaction before the content-hash check.
            # This matches the raw backend and makes concurrent same-identity
            # writes resolve deterministically as created then reused.
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            memory = (
                session.query(LongTermMemoryModel)
                .filter(
                    LongTermMemoryModel.id == memory_id,
                    LongTermMemoryModel.user_id == user_id,
                )
                .limit(1)
                .one_or_none()
            )
            if memory is None:
                raise ValueError("memory_embedding_artifact_target_not_found")
            if canonical_content_hash(memory.content) != prepared.content_hash:
                raise EmbeddingArtifactValidationError("content_hash_target_mismatch")

            identity = (
                memory_id,
                prepared.signature_fingerprint,
                prepared.purpose,
            )
            existing = session.get(LongTermMemoryEmbeddingArtifactModel, identity)
            if existing is None:
                session.add(
                    LongTermMemoryEmbeddingArtifactModel(
                        memory_id=memory_id,
                        user_id=user_id,
                        embedding_signature_json=prepared.signature_json,
                        embedding_signature_fingerprint=prepared.signature_fingerprint,
                        embedding_purpose=prepared.purpose,
                        embedding_content_hash=prepared.content_hash,
                        embedding_vector_format=prepared.vector_format,
                        vector_dim=prepared.vector_dim,
                        embedding=prepared.vector_bytes,
                        embedding_vector_sha256=prepared.vector_sha256,
                        created_at=utc_now(),
                        updated_at=None,
                    )
                )
                session.commit()
                return EmbeddingArtifactUpsertOutcome(status="created")

            if existing.embedding_signature_json != prepared.signature_json:
                raise EmbeddingArtifactValidationError("signature_fingerprint_collision")
            if existing.user_id != user_id:
                raise ValueError("memory_embedding_artifact_owner_mismatch")
            if existing.embedding_content_hash == prepared.content_hash:
                diagnostics = ()
                if existing.embedding_vector_sha256 != prepared.vector_sha256:
                    diagnostics = diagnostics_from_reasons(["incoming_vector_differs"])
                session.commit()
                return EmbeddingArtifactUpsertOutcome(status="reused", diagnostics=diagnostics)

            existing.embedding_content_hash = prepared.content_hash
            existing.embedding_vector_format = prepared.vector_format
            existing.vector_dim = prepared.vector_dim
            existing.embedding = prepared.vector_bytes
            existing.embedding_vector_sha256 = prepared.vector_sha256
            existing.updated_at = utc_now()
            session.commit()
            return EmbeddingArtifactUpsertOutcome(status="replaced")

    def search_memory_embedding_artifacts(
        self,
        *,
        eligibility: MemoryArtifactEligibility,
        query_signature: EmbeddingModelSignature,
        query_embedding: np.ndarray,
        limit: int,
        candidate_limit: int | None = None,
    ) -> MemoryEmbeddingSearchOutcome:
        candidate_limit = candidate_limit or max(limit * 4, limit + 8)
        query = prepare_embedding_query(
            signature=query_signature,
            vector=query_embedding,
        )
        legacy_hash_signature = deterministic_hash_v1_legacy_signature()
        with self._session_factory() as session:
            session.begin()
            memory_filters = self._eligibility_filters(eligibility)
            artifact_filters = (
                LongTermMemoryEmbeddingArtifactModel.user_id == eligibility.user_id,
                *memory_filters,
            )
            eligible_memory_count = (
                session.query(LongTermMemoryModel.id)
                .filter(*memory_filters)
                .count()
            )
            rows = (
                session.query(LongTermMemoryEmbeddingArtifactModel, LongTermMemoryModel.content)
                .join(
                    LongTermMemoryModel,
                    LongTermMemoryModel.id == LongTermMemoryEmbeddingArtifactModel.memory_id,
                )
                .filter(
                    *artifact_filters,
                    LongTermMemoryEmbeddingArtifactModel.embedding_signature_fingerprint
                    == query.signature_fingerprint,
                    LongTermMemoryEmbeddingArtifactModel.embedding_purpose == "document",
                )
                .all()
            )
            artifact_count = (
                session.query(LongTermMemoryEmbeddingArtifactModel.memory_id)
                .join(
                    LongTermMemoryModel,
                    LongTermMemoryModel.id == LongTermMemoryEmbeddingArtifactModel.memory_id,
                )
                .filter(
                    *artifact_filters,
                    LongTermMemoryEmbeddingArtifactModel.embedding_purpose == "document",
                )
                .count()
            )
            incompatible_signature_count = (
                session.query(LongTermMemoryEmbeddingArtifactModel.memory_id)
                .join(
                    LongTermMemoryModel,
                    LongTermMemoryModel.id == LongTermMemoryEmbeddingArtifactModel.memory_id,
                )
                .filter(
                    *artifact_filters,
                    LongTermMemoryEmbeddingArtifactModel.embedding_purpose == "document",
                    LongTermMemoryEmbeddingArtifactModel.embedding_signature_fingerprint
                    != query.signature_fingerprint,
                )
                .count()
            )
            legacy_rows = (
                session.query(
                    LongTermMemoryEmbeddingModel.memory_id,
                    LongTermMemoryEmbeddingModel.embedding_model,
                    LongTermMemoryEmbeddingModel.vector_dim,
                    LongTermMemoryEmbeddingModel.embedding,
                    LongTermMemoryModel.content,
                    LongTermMemoryEmbeddingArtifactModel.embedding_signature_json,
                    LongTermMemoryEmbeddingArtifactModel.embedding_signature_fingerprint,
                    LongTermMemoryEmbeddingArtifactModel.embedding_purpose,
                    LongTermMemoryEmbeddingArtifactModel.embedding_content_hash,
                    LongTermMemoryEmbeddingArtifactModel.embedding_vector_format,
                    LongTermMemoryEmbeddingArtifactModel.vector_dim,
                    LongTermMemoryEmbeddingArtifactModel.embedding,
                    LongTermMemoryEmbeddingArtifactModel.embedding_vector_sha256,
                )
                .join(
                    LongTermMemoryModel,
                    LongTermMemoryModel.id == LongTermMemoryEmbeddingModel.memory_id,
                )
                .outerjoin(
                    LongTermMemoryEmbeddingArtifactModel,
                    and_(
                        LongTermMemoryEmbeddingArtifactModel.memory_id
                        == LongTermMemoryEmbeddingModel.memory_id,
                        LongTermMemoryEmbeddingArtifactModel.user_id
                        == LongTermMemoryEmbeddingModel.user_id,
                        LongTermMemoryEmbeddingArtifactModel.embedding_signature_fingerprint
                        == legacy_hash_signature.fingerprint(),
                        LongTermMemoryEmbeddingArtifactModel.embedding_purpose == "document",
                    ),
                )
                .filter(
                    LongTermMemoryEmbeddingModel.user_id == eligibility.user_id,
                    *memory_filters,
                )
                .all()
            )
            session.commit()

        reasons: list[str] = ["signature_mismatch"] * incompatible_signature_count
        results: list[MemoryVectorSearchResult] = []
        compatible_count = 0
        for artifact, content in rows:
            if artifact.embedding_signature_json != query.signature_json:
                reasons.append("signature_mismatch")
                continue
            try:
                _signature, vector = validate_stored_embedding_artifact(
                    signature_json=artifact.embedding_signature_json,
                    signature_fingerprint=artifact.embedding_signature_fingerprint,
                    content_hash=artifact.embedding_content_hash,
                    purpose=artifact.embedding_purpose,
                    vector_format=artifact.embedding_vector_format,
                    vector_dim=artifact.vector_dim,
                    vector_bytes=artifact.embedding,
                    vector_sha256=artifact.embedding_vector_sha256,
                )
            except EmbeddingArtifactValidationError as exc:
                reason = (
                    "signature_corrupt"
                    if exc.reason.startswith("signature_")
                    else "vector_corrupt"
                )
                reasons.append(reason)
                continue
            if canonical_content_hash(content) != artifact.embedding_content_hash:
                reasons.append("content_hash_stale")
                continue
            compatible_count += 1
            results.append(
                MemoryVectorSearchResult(
                    memory_id=artifact.memory_id,
                    score=float(np.dot(query.vector, vector)),
                )
            )
        reasons.extend(
            "legacy_unverified"
            for (
                _memory_id,
                embedding_model,
                vector_dim,
                legacy_embedding,
                content,
                artifact_signature_json,
                artifact_signature_fingerprint,
                artifact_purpose,
                artifact_content_hash,
                artifact_vector_format,
                artifact_vector_dim,
                artifact_embedding,
                artifact_vector_sha256,
            ) in legacy_rows
            if not is_promoted_deterministic_hash_legacy(
                embedding_model=embedding_model,
                vector_dim=vector_dim,
                legacy_vector_bytes=legacy_embedding,
                target_content=content,
                artifact_signature_json=artifact_signature_json,
                artifact_signature_fingerprint=artifact_signature_fingerprint,
                artifact_purpose=artifact_purpose,
                artifact_content_hash=artifact_content_hash,
                artifact_vector_format=artifact_vector_format,
                artifact_vector_dim=artifact_vector_dim,
                artifact_vector_bytes=artifact_embedding,
                artifact_vector_sha256=artifact_vector_sha256,
            )
        )
        results.sort(key=lambda item: item.score, reverse=True)
        return MemoryEmbeddingSearchOutcome(
            hits=results[:candidate_limit],
            diagnostics=EmbeddingArtifactDiagnostics(
                scanned_count=artifact_count + len(legacy_rows),
                compatible_count=compatible_count,
                issues=diagnostics_from_reasons(reasons),
            ),
            eligible_memory_count=eligible_memory_count,
        )

    def list_memory_embedding_artifact_gaps(
        self,
        *,
        query_signature: EmbeddingModelSignature,
        user_id: str | None = None,
        cursor: MemoryEmbeddingArtifactGapCursor | None = None,
        limit: int = 50,
    ) -> MemoryEmbeddingArtifactGapPage:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= _MAX_MEMORY_ARTIFACT_GAP_LIMIT
        ):
            raise ValueError("memory_artifact_gap_limit_invalid")
        scan_limit = min(limit * 4, _MAX_MEMORY_ARTIFACT_GAP_SCAN_LIMIT)
        signature_json = query_signature.canonical()
        signature_fingerprint = query_signature.fingerprint()
        exact_artifact = aliased(LongTermMemoryEmbeddingArtifactModel)
        any_artifact = aliased(LongTermMemoryEmbeddingArtifactModel)
        legacy_embedding = aliased(LongTermMemoryEmbeddingModel)

        filters = [
            LongTermMemoryModel.status == "active",
            LongTermMemoryModel.is_active == 1,
        ]
        if user_id is not None:
            filters.append(LongTermMemoryModel.user_id == user_id)
        if cursor is not None:
            filters.append(
                or_(
                    LongTermMemoryModel.created_at > cursor.created_at,
                    and_(
                        LongTermMemoryModel.created_at == cursor.created_at,
                        LongTermMemoryModel.id > cursor.memory_id,
                    ),
                )
            )

        with self._session_factory() as session:
            session.begin()
            has_any_canonical_artifact = (
                session.query(any_artifact.memory_id)
                .filter(
                    any_artifact.memory_id == LongTermMemoryModel.id,
                    any_artifact.user_id == LongTermMemoryModel.user_id,
                    any_artifact.embedding_purpose == "document",
                )
                .correlate(LongTermMemoryModel)
                .exists()
            )
            has_legacy_embedding = (
                session.query(legacy_embedding.memory_id)
                .filter(
                    legacy_embedding.memory_id == LongTermMemoryModel.id,
                    legacy_embedding.user_id == LongTermMemoryModel.user_id,
                )
                .correlate(LongTermMemoryModel)
                .exists()
            )
            rows = (
                session.query(
                    LongTermMemoryModel.id.label("memory_id"),
                    LongTermMemoryModel.user_id,
                    LongTermMemoryModel.content,
                    LongTermMemoryModel.created_at,
                    exact_artifact.embedding_signature_json.label(
                        "artifact_signature_json"
                    ),
                    exact_artifact.embedding_signature_fingerprint.label(
                        "artifact_signature_fingerprint"
                    ),
                    exact_artifact.embedding_purpose.label("artifact_purpose"),
                    exact_artifact.embedding_content_hash.label("artifact_content_hash"),
                    exact_artifact.embedding_vector_format.label(
                        "artifact_vector_format"
                    ),
                    exact_artifact.vector_dim.label("artifact_vector_dim"),
                    exact_artifact.embedding.label("artifact_embedding"),
                    exact_artifact.embedding_vector_sha256.label(
                        "artifact_vector_sha256"
                    ),
                    has_any_canonical_artifact.label("has_any_canonical_artifact"),
                    has_legacy_embedding.label("has_legacy_embedding"),
                )
                .outerjoin(
                    exact_artifact,
                    and_(
                        exact_artifact.memory_id == LongTermMemoryModel.id,
                        exact_artifact.user_id == LongTermMemoryModel.user_id,
                        exact_artifact.embedding_signature_fingerprint
                        == signature_fingerprint,
                        exact_artifact.embedding_purpose == "document",
                    ),
                )
                .filter(*filters)
                .order_by(LongTermMemoryModel.created_at.asc(), LongTermMemoryModel.id.asc())
                .limit(scan_limit + 1)
                .all()
            )
            session.commit()

        candidates = rows[:scan_limit]
        has_more_candidates = len(rows) > scan_limit
        gaps: list[MemoryEmbeddingArtifactGap] = []
        checked = 0
        next_cursor: MemoryEmbeddingArtifactGapCursor | None = None
        for row in candidates:
            checked += 1
            artifact = None
            if row.artifact_signature_fingerprint is not None:
                artifact = StoredMemoryEmbeddingArtifact(
                    signature_json=row.artifact_signature_json,
                    signature_fingerprint=row.artifact_signature_fingerprint,
                    purpose=row.artifact_purpose,
                    content_hash=row.artifact_content_hash,
                    vector_format=row.artifact_vector_format,
                    vector_dim=row.artifact_vector_dim,
                    vector_bytes=row.artifact_embedding,
                    vector_sha256=row.artifact_vector_sha256,
                )
            reason = classify_memory_embedding_artifact_gap(
                target_content=row.content,
                expected_signature_json=signature_json,
                artifact=artifact,
                has_any_canonical_artifact=bool(row.has_any_canonical_artifact),
                has_legacy_embedding=bool(row.has_legacy_embedding),
            )
            if reason is not None:
                gaps.append(
                    MemoryEmbeddingArtifactGap(
                        memory_id=row.memory_id,
                        user_id=row.user_id,
                        content=row.content,
                        reason=reason,
                        created_at=row.created_at,
                    )
                )
            if len(gaps) == limit:
                if checked < len(candidates) or has_more_candidates:
                    next_cursor = MemoryEmbeddingArtifactGapCursor(
                        created_at=row.created_at,
                        memory_id=row.memory_id,
                    )
                break
        else:
            if candidates and has_more_candidates:
                last = candidates[-1]
                next_cursor = MemoryEmbeddingArtifactGapCursor(
                    created_at=last.created_at,
                    memory_id=last.memory_id,
                )
        return MemoryEmbeddingArtifactGapPage(
            gaps=gaps,
            next_cursor=next_cursor,
            scanned_memory_count=checked,
        )
