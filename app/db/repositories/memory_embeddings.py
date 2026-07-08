from __future__ import annotations

import numpy as np

from app.db.models import LongTermMemoryEmbeddingModel, LongTermMemoryModel
from app.db.repositories.common import SessionFactory
from app.memory.schemas import MemoryVectorSearchResult
from app.utils.ids import utc_now


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
