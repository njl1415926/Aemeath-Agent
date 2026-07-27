from __future__ import annotations

import numpy as np

from app.embeddings.errors import EmbeddingInvalidInputError, EmbeddingInvalidResponseError
from app.embeddings.schemas import EmbeddingRequest


def validate_request(request: EmbeddingRequest) -> None:
    if request.purpose not in {"query", "document"}:
        raise EmbeddingInvalidInputError("unsupported_purpose")
    if not isinstance(request.texts, tuple):
        raise EmbeddingInvalidInputError("texts_must_be_tuple")
    for text in request.texts:
        if not isinstance(text, str):
            raise EmbeddingInvalidInputError("text_must_be_string")
        if not text.strip():
            raise EmbeddingInvalidInputError("blank_text")


def empty_vectors(dimension: int) -> np.ndarray:
    return np.empty((0, dimension), dtype="<f4", order="C")


def normalize_and_validate_vectors(
    vectors: object,
    *,
    input_count: int,
    dimension: int,
    reason_prefix: str = "provider_output",
) -> np.ndarray:
    try:
        array = np.asarray(vectors, dtype="<f4", order="C")
    except (TypeError, ValueError, OverflowError) as exc:
        raise EmbeddingInvalidResponseError(f"{reason_prefix}_not_numeric") from exc
    if array.ndim != 2:
        raise EmbeddingInvalidResponseError(f"{reason_prefix}_rank")
    if array.shape != (input_count, dimension):
        raise EmbeddingInvalidResponseError(f"{reason_prefix}_shape")
    if not np.all(np.isfinite(array)):
        raise EmbeddingInvalidResponseError(f"{reason_prefix}_non_finite")
    if input_count == 0:
        return empty_vectors(dimension)
    norms = np.linalg.norm(array.astype(np.float64), axis=1)
    if np.any(~np.isfinite(norms)) or np.any(norms <= 0.0):
        raise EmbeddingInvalidResponseError(f"{reason_prefix}_zero_norm")
    normalized = array.astype(np.float64) / norms[:, np.newaxis]
    result = np.ascontiguousarray(normalized, dtype="<f4")
    result_norms = np.linalg.norm(result.astype(np.float64), axis=1)
    if np.any(np.abs(result_norms - 1.0) > 1e-5):
        raise EmbeddingInvalidResponseError(f"{reason_prefix}_normalization")
    return result


def validate_normalized_vectors(
    vectors: object,
    *,
    input_count: int,
    dimension: int,
    norm_tolerance: float = 1e-5,
) -> np.ndarray:
    array = normalize_and_validate_vectors(
        vectors,
        input_count=input_count,
        dimension=dimension,
        reason_prefix="vector_artifact",
    )
    original = np.asarray(vectors, dtype="<f4", order="C")
    norms = np.linalg.norm(original.astype(np.float64), axis=1)
    if np.any(np.abs(norms - 1.0) > norm_tolerance):
        raise EmbeddingInvalidResponseError("vector_artifact_not_normalized")
    return array
