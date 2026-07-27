from __future__ import annotations

import hashlib
import json
import math
import re
from importlib import resources
from pathlib import PurePosixPath
from typing import Any

import numpy as np

from app.embeddings.errors import EmbeddingConfigurationError
from app.embeddings.fingerprints import expected_qualification_fingerprint
from app.embeddings.schemas import (
    CanaryItem,
    CanarySet,
    EmbeddingSpaceProfile,
    QualificationThresholds,
)

_PROFILE_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_PROFILE_FIELDS = {
    "schema_version",
    "release_status",
    "profile_id",
    "provider",
    "model_id",
    "dimension",
    "model_revision",
    "pooling",
    "normalization",
    "query_instruction",
    "document_instruction",
    "preprocessing_version",
    "postprocessing_version",
    "canary_set_version",
    "canary_artifact",
    "canary_artifact_sha256",
    "baseline_artifact",
    "baseline_artifact_sha256",
    "fingerprint_algorithm",
    "expected_fingerprint",
    "qualification_thresholds",
}
_THRESHOLD_FIELDS = {"min_cosine_per_vector", "max_abs_coordinate_delta"}
_CANARY_SET_FIELDS = {"schema_version", "canary_set_version", "canaries"}
_CANARY_ITEM_FIELDS = {"id", "text"}


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise EmbeddingConfigurationError("profile_json_duplicate_key")
        result[key] = value
    return result


def _load_json_bytes(raw: bytes, *, reason_prefix: str) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except EmbeddingConfigurationError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EmbeddingConfigurationError(f"{reason_prefix}_invalid_json") from exc
    if not isinstance(value, dict):
        raise EmbeddingConfigurationError(f"{reason_prefix}_not_object")
    return value


def _require_exact_fields(value: dict[str, Any], fields: set[str], reason: str) -> None:
    if set(value) != fields:
        raise EmbeddingConfigurationError(reason)


def _require_string(value: Any, reason: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise EmbeddingConfigurationError(reason)
    return value


def _require_int(value: Any, reason: str, *, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise EmbeddingConfigurationError(reason)
    return value


def _require_number(value: Any, reason: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EmbeddingConfigurationError(reason)
    number = float(value)
    if not math.isfinite(number):
        raise EmbeddingConfigurationError(reason)
    return number


def _validate_digest(value: str, reason: str) -> str:
    if not _SHA256_RE.fullmatch(value):
        raise EmbeddingConfigurationError(reason)
    return value


def _validate_artifact_path(value: str, *, allowed_root: str, reason: str) -> str:
    if "\\" in value or ":" in value:
        raise EmbeddingConfigurationError(reason)
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or path.parts[0] != allowed_root:
        raise EmbeddingConfigurationError(reason)
    if any(part in {"", ".", ".."} for part in path.parts):
        raise EmbeddingConfigurationError(reason)
    return value


def _profile_root():
    return resources.files("app.embeddings.profiles")


def read_profile_artifact(path: str, expected_sha256: str, *, allowed_root: str) -> bytes:
    safe_path = _validate_artifact_path(
        path,
        allowed_root=allowed_root,
        reason="profile_artifact_path_invalid",
    )
    _validate_digest(expected_sha256, "profile_artifact_digest_invalid")
    resource = _profile_root()
    for part in PurePosixPath(safe_path).parts:
        resource = resource.joinpath(part)
    try:
        raw = resource.read_bytes()
    except (FileNotFoundError, IsADirectoryError) as exc:
        raise EmbeddingConfigurationError("profile_artifact_missing") from exc
    observed = f"sha256:{hashlib.sha256(raw).hexdigest()}"
    if observed != expected_sha256:
        raise EmbeddingConfigurationError("profile_artifact_digest_mismatch")
    return raw


def _parse_thresholds(value: Any) -> QualificationThresholds | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise EmbeddingConfigurationError("qualification_thresholds_not_object")
    _require_exact_fields(value, _THRESHOLD_FIELDS, "qualification_thresholds_fields")
    min_cosine = _require_number(value["min_cosine_per_vector"], "min_cosine_invalid")
    max_delta = _require_number(
        value["max_abs_coordinate_delta"],
        "max_coordinate_delta_invalid",
    )
    if not -1.0 <= min_cosine <= 1.0 or max_delta < 0.0:
        raise EmbeddingConfigurationError("qualification_thresholds_range")
    return QualificationThresholds(
        min_cosine_per_vector=min_cosine,
        max_abs_coordinate_delta=max_delta,
    )


def _parse_profile(raw: bytes, expected_profile_id: str) -> EmbeddingSpaceProfile:
    data = _load_json_bytes(raw, reason_prefix="profile")
    _require_exact_fields(data, _PROFILE_FIELDS, "profile_fields_invalid")
    profile_id = _require_string(data["profile_id"], "profile_id_invalid")
    if profile_id != expected_profile_id or not _PROFILE_ID_RE.fullmatch(profile_id):
        raise EmbeddingConfigurationError("profile_id_mismatch")
    release_status = _require_string(data["release_status"], "release_status_invalid")
    if release_status not in {"review", "approved"}:
        raise EmbeddingConfigurationError("release_status_invalid")
    provider = _require_string(data["provider"], "provider_invalid")
    if provider not in {"siliconflow", "local_bge_m3", "deterministic_hash"}:
        raise EmbeddingConfigurationError("provider_invalid")
    baseline_artifact = data["baseline_artifact"]
    baseline_digest = data["baseline_artifact_sha256"]
    expected_fingerprint = data["expected_fingerprint"]
    for value, reason in (
        (baseline_artifact, "baseline_artifact_invalid"),
        (baseline_digest, "baseline_digest_invalid"),
        (expected_fingerprint, "expected_fingerprint_invalid"),
    ):
        if value is not None and not isinstance(value, str):
            raise EmbeddingConfigurationError(reason)
    if baseline_artifact is not None:
        baseline_artifact = _validate_artifact_path(
            baseline_artifact,
            allowed_root="baselines",
            reason="baseline_artifact_invalid",
        )
    if baseline_digest is not None:
        baseline_digest = _validate_digest(baseline_digest, "baseline_digest_invalid")
    if expected_fingerprint is not None:
        expected_fingerprint = _validate_digest(
            expected_fingerprint,
            "expected_fingerprint_invalid",
        )
    thresholds = _parse_thresholds(data["qualification_thresholds"])
    qualification_fields = (
        baseline_artifact,
        baseline_digest,
        expected_fingerprint,
        thresholds,
    )
    if any(value is None for value in qualification_fields) and any(
        value is not None for value in qualification_fields
    ):
        raise EmbeddingConfigurationError("qualification_fields_incomplete")
    if release_status == "approved" and any(value is None for value in qualification_fields):
        raise EmbeddingConfigurationError("approved_profile_not_qualified")
    profile = EmbeddingSpaceProfile(
        schema_version=_require_string(data["schema_version"], "schema_version_invalid"),
        release_status=release_status,  # type: ignore[arg-type]
        profile_id=profile_id,
        provider=provider,  # type: ignore[arg-type]
        model_id=_require_string(data["model_id"], "model_id_invalid"),
        dimension=_require_int(data["dimension"], "dimension_invalid"),
        model_revision=_require_string(data["model_revision"], "model_revision_invalid"),
        pooling=_require_string(data["pooling"], "pooling_invalid"),
        normalization=_require_string(data["normalization"], "normalization_invalid"),
        query_instruction=_require_string(
            data["query_instruction"],
            "query_instruction_invalid",
            allow_empty=True,
        ),
        document_instruction=_require_string(
            data["document_instruction"],
            "document_instruction_invalid",
            allow_empty=True,
        ),
        preprocessing_version=_require_string(
            data["preprocessing_version"],
            "preprocessing_version_invalid",
        ),
        postprocessing_version=_require_string(
            data["postprocessing_version"],
            "postprocessing_version_invalid",
        ),
        canary_set_version=_require_string(
            data["canary_set_version"],
            "canary_set_version_invalid",
        ),
        canary_artifact=_validate_artifact_path(
            _require_string(data["canary_artifact"], "canary_artifact_invalid"),
            allowed_root="canaries",
            reason="canary_artifact_invalid",
        ),
        canary_artifact_sha256=_validate_digest(
            _require_string(data["canary_artifact_sha256"], "canary_digest_invalid"),
            "canary_digest_invalid",
        ),
        baseline_artifact=baseline_artifact,
        baseline_artifact_sha256=baseline_digest,
        fingerprint_algorithm=_require_string(
            data["fingerprint_algorithm"],
            "fingerprint_algorithm_invalid",
        ),
        expected_fingerprint=expected_fingerprint,
        qualification_thresholds=thresholds,
    )
    _validate_profile_contract(profile)
    return profile


def _validate_profile_contract(profile: EmbeddingSpaceProfile) -> None:
    if profile.schema_version != "embedding-space-profile/v1":
        raise EmbeddingConfigurationError("unsupported_profile_schema")
    if profile.normalization != "l2":
        raise EmbeddingConfigurationError("unsupported_normalization")
    if profile.fingerprint_algorithm != "baseline-relative-mask-v1":
        raise EmbeddingConfigurationError("unsupported_fingerprint_algorithm")
    if profile.query_instruction or profile.document_instruction:
        raise EmbeddingConfigurationError("bge_instruction_must_be_empty")
    if profile.provider == "siliconflow":
        expected = ("Pro/BAAI/bge-m3", 1024, "provider_managed", "provider_managed")
    elif profile.provider == "local_bge_m3":
        expected = ("BAAI/bge-m3", 1024, "provider_managed", "cls")
    else:
        expected = (
            "deterministic_hash_v1",
            384,
            "sha1-signed-token-bucket-v1",
            "signed_token_bucket",
        )
    actual = (
        profile.model_id,
        profile.dimension,
        profile.model_revision,
        profile.pooling,
    )
    if actual != expected:
        raise EmbeddingConfigurationError("provider_profile_contract_mismatch")


def load_profile_for_qualification(profile_id: str) -> EmbeddingSpaceProfile:
    if not isinstance(profile_id, str) or not _PROFILE_ID_RE.fullmatch(profile_id):
        raise EmbeddingConfigurationError("profile_id_invalid")
    try:
        raw = _profile_root().joinpath(f"{profile_id}.json").read_bytes()
    except (FileNotFoundError, IsADirectoryError) as exc:
        raise EmbeddingConfigurationError("profile_not_found") from exc
    profile = _parse_profile(raw, profile_id)
    canaries = load_canary_set(profile)
    if profile.baseline_artifact is not None:
        baseline = load_baseline(profile, canary_count=len(canaries.items))
        if profile.expected_fingerprint is None:
            raise EmbeddingConfigurationError("expected_fingerprint_missing")
        recomputed = expected_qualification_fingerprint(
            profile,
            canary_count=baseline.shape[0],
        )
        if recomputed != profile.expected_fingerprint:
            raise EmbeddingConfigurationError("expected_fingerprint_mismatch")
    return profile


def load_embedding_space_profile(profile_id: str) -> EmbeddingSpaceProfile:
    profile = load_profile_for_qualification(profile_id)
    if profile.release_status != "approved":
        raise EmbeddingConfigurationError("profile_not_approved")
    return profile


load_approved_embedding_space_profile = load_embedding_space_profile


def load_canary_set(profile: EmbeddingSpaceProfile) -> CanarySet:
    raw = read_profile_artifact(
        profile.canary_artifact,
        profile.canary_artifact_sha256,
        allowed_root="canaries",
    )
    data = _load_json_bytes(raw, reason_prefix="canary_set")
    _require_exact_fields(data, _CANARY_SET_FIELDS, "canary_set_fields_invalid")
    if data["schema_version"] != "embedding-canary-set/v1":
        raise EmbeddingConfigurationError("canary_set_schema_invalid")
    if data["canary_set_version"] != profile.canary_set_version:
        raise EmbeddingConfigurationError("canary_set_version_mismatch")
    raw_items = data["canaries"]
    if not isinstance(raw_items, list) or not raw_items:
        raise EmbeddingConfigurationError("canary_items_invalid")
    items: list[CanaryItem] = []
    seen: set[str] = set()
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            raise EmbeddingConfigurationError("canary_item_not_object")
        _require_exact_fields(raw_item, _CANARY_ITEM_FIELDS, "canary_item_fields_invalid")
        canary_id = _require_string(raw_item["id"], "canary_id_invalid")
        text = _require_string(raw_item["text"], "canary_text_invalid")
        if canary_id in seen or not text.strip():
            raise EmbeddingConfigurationError("canary_item_invalid")
        seen.add(canary_id)
        items.append(CanaryItem(canary_id=canary_id, text=text))
    return CanarySet(
        schema_version=data["schema_version"],
        canary_set_version=data["canary_set_version"],
        items=tuple(items),
    )


def load_baseline(profile: EmbeddingSpaceProfile, *, canary_count: int) -> np.ndarray:
    if profile.baseline_artifact is None or profile.baseline_artifact_sha256 is None:
        raise EmbeddingConfigurationError("profile_baseline_missing")
    raw = read_profile_artifact(
        profile.baseline_artifact,
        profile.baseline_artifact_sha256,
        allowed_root="baselines",
    )
    expected_bytes = canary_count * profile.dimension * 4
    if len(raw) != expected_bytes:
        raise EmbeddingConfigurationError("baseline_byte_length_mismatch")
    baseline = np.frombuffer(raw, dtype="<f4").reshape(canary_count, profile.dimension).copy()
    if not np.all(np.isfinite(baseline)):
        raise EmbeddingConfigurationError("baseline_non_finite")
    norms = np.linalg.norm(baseline.astype(np.float64), axis=1)
    if np.any(norms <= 0.0) or np.any(np.abs(norms - 1.0) > 1e-5):
        raise EmbeddingConfigurationError("baseline_not_normalized")
    return np.ascontiguousarray(baseline, dtype="<f4")
