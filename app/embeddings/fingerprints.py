from __future__ import annotations

import hashlib
import json

import numpy as np

from app.embeddings.errors import EmbeddingQualificationError
from app.embeddings.schemas import EmbeddingSpaceProfile

_FINGERPRINT_DOMAIN = b"AEMEATH_EMBEDDING_QUALIFICATION_V1\x00"


def qualification_fingerprint(profile: EmbeddingSpaceProfile, coordinate_mask: np.ndarray) -> str:
    thresholds = profile.qualification_thresholds
    if thresholds is None or profile.baseline_artifact_sha256 is None:
        raise EmbeddingQualificationError("profile_has_no_qualification_baseline")
    mask = np.asarray(coordinate_mask, dtype=np.bool_, order="C")
    if mask.ndim != 2 or mask.shape[1] != profile.dimension:
        raise EmbeddingQualificationError("coordinate_mask_shape")
    header = {
        "algorithm": profile.fingerprint_algorithm,
        "baseline_artifact_sha256": profile.baseline_artifact_sha256,
        "canary_artifact_sha256": profile.canary_artifact_sha256,
        "max_abs_coordinate_delta": format(
            thresholds.max_abs_coordinate_delta,
            ".17g",
        ),
        "min_cosine_per_vector": format(thresholds.min_cosine_per_vector, ".17g"),
        "profile_id": profile.profile_id,
        "shape": [int(mask.shape[0]), int(mask.shape[1])],
    }
    canonical_header = json.dumps(
        header,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    packed = np.packbits(mask.reshape(-1), bitorder="little").tobytes()
    digest = hashlib.sha256(_FINGERPRINT_DOMAIN + canonical_header + b"\x00" + packed)
    return f"sha256:{digest.hexdigest()}"


def expected_qualification_fingerprint(
    profile: EmbeddingSpaceProfile,
    *,
    canary_count: int,
) -> str:
    mask = np.zeros((canary_count, profile.dimension), dtype=np.bool_)
    return qualification_fingerprint(profile, mask)
