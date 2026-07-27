from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Callable

import numpy as np

from app.embeddings.errors import EmbeddingQualificationError
from app.embeddings.fingerprints import (
    expected_qualification_fingerprint,
    qualification_fingerprint,
)
from app.embeddings.ports import EmbeddingProviderPort
from app.embeddings.schemas import (
    CanarySet,
    EmbeddingRequest,
    EmbeddingSpaceProfile,
    QualificationResult,
    QualificationThresholds,
    QualificationVectorMetrics,
)

_VECTOR_NORM_TOLERANCE = 1e-5
_CANDIDATE_BASELINE_NAME = "baseline.candidate.f32le"
_CAPTURE_REPORT_NAME = "capture_report.json"
_PROFILE_PATCH_NAME = "profile_patch.candidate.json"


@dataclass(frozen=True)
class QualificationObservation:
    """One independently checked provider observation.

    The vector coordinates intentionally stay out of this schema so callers cannot
    accidentally serialize them into a report or log.
    """

    observation_id: str
    duration_ms: float
    norm_min: float
    norm_max: float
    result: QualificationResult


@dataclass(frozen=True)
class QualificationProbeResult:
    passed: bool
    profile_id: str
    provider: str
    model_id: str
    signature_fingerprint: str
    dimension: int
    canary_count: int
    logical_batch_size: int
    observations: tuple[QualificationObservation, ...]


@dataclass(frozen=True)
class CandidateCaptureResult:
    output_dir: Path
    baseline_artifact_sha256: str
    expected_fingerprint: str
    probe: QualificationProbeResult


def compare_observation_to_baseline(
    *,
    profile: EmbeddingSpaceProfile,
    canary_set: CanarySet,
    baseline: np.ndarray,
    observed: np.ndarray,
) -> QualificationResult:
    """Compare one complete observation with the committed coordinate baseline.

    All arithmetic that decides compatibility is float64.  Fingerprints are derived
    from the row-major mask ``abs(observed - baseline) > threshold``; the packing and
    domain separation are centralized in :mod:`app.embeddings.fingerprints`.
    """

    thresholds = profile.qualification_thresholds
    if (
        thresholds is None
        or profile.baseline_artifact_sha256 is None
        or profile.expected_fingerprint is None
    ):
        raise EmbeddingQualificationError("profile_has_no_qualification_baseline")
    if canary_set.canary_set_version != profile.canary_set_version:
        raise EmbeddingQualificationError("canary_set_version_mismatch")

    canary_count = len(canary_set.items)
    baseline_array = _validate_vectors(
        baseline,
        canary_count=canary_count,
        dimension=profile.dimension,
        reason_prefix="baseline",
    )
    observed_array = _validate_vectors(
        observed,
        canary_count=canary_count,
        dimension=profile.dimension,
        reason_prefix="observed",
    )

    expected = expected_qualification_fingerprint(profile, canary_count=canary_count)
    if expected != profile.expected_fingerprint:
        raise EmbeddingQualificationError("expected_fingerprint_mismatch")

    baseline64 = baseline_array.astype(np.float64)
    observed64 = observed_array.astype(np.float64)
    absolute_delta = np.abs(observed64 - baseline64)
    coordinate_mask = absolute_delta > thresholds.max_abs_coordinate_delta
    observed_fingerprint = qualification_fingerprint(profile, coordinate_mask)

    dot_products = np.sum(baseline64 * observed64, axis=1, dtype=np.float64)
    baseline_norms = np.linalg.norm(baseline64, axis=1)
    observed_norms = np.linalg.norm(observed64, axis=1)
    cosines = dot_products / (baseline_norms * observed_norms)
    cosines = np.clip(cosines, -1.0, 1.0)
    max_deltas = np.max(absolute_delta, axis=1)

    metrics = tuple(
        QualificationVectorMetrics(
            canary_id=item.canary_id,
            cosine=float(cosines[index]),
            max_abs_coordinate_delta=float(max_deltas[index]),
        )
        for index, item in enumerate(canary_set.items)
    )
    passed = (
        profile.expected_fingerprint == observed_fingerprint
        and all(
            metric.cosine >= thresholds.min_cosine_per_vector
            and metric.max_abs_coordinate_delta
            <= thresholds.max_abs_coordinate_delta
            for metric in metrics
        )
    )
    return QualificationResult(
        passed=passed,
        expected_fingerprint=profile.expected_fingerprint,
        observed_fingerprint=observed_fingerprint,
        metrics=metrics,
    )


# A concise alias for callers that do not need to know the baseline implementation.
qualify_observation = compare_observation_to_baseline


def run_qualification_probe(
    *,
    profile: EmbeddingSpaceProfile,
    canary_set: CanarySet,
    baseline: np.ndarray,
    provider: EmbeddingProviderPort,
    repeat_count: int = 2,
    clock: Callable[[], float] = time.perf_counter,
    allow_unqualified_signature: bool = False,
) -> QualificationProbeResult:
    """Run independent batch/repeat/single-order observations against a baseline."""

    if isinstance(repeat_count, bool) or not isinstance(repeat_count, int) or repeat_count < 2:
        raise EmbeddingQualificationError("repeat_count_must_be_at_least_two")
    signature_matches = provider.signature.canonical() == profile.signature.canonical()
    if allow_unqualified_signature:
        signature_matches = _space_signature_matches(provider.signature, profile.signature)
    if not signature_matches:
        raise EmbeddingQualificationError("provider_signature_mismatch")

    # Reject a bad baseline/profile before invoking a local model or paid API.
    baseline = _validate_vectors(
        baseline,
        canary_count=len(canary_set.items),
        dimension=profile.dimension,
        reason_prefix="baseline",
    )
    compare_observation_to_baseline(
        profile=profile,
        canary_set=canary_set,
        baseline=baseline,
        observed=baseline,
    )

    texts = tuple(item.text for item in canary_set.items)
    observations: list[tuple[str, np.ndarray, float]] = []
    for index in range(repeat_count):
        vectors, duration_ms = _embed_timed(
            provider,
            EmbeddingRequest(texts=texts, purpose="document"),
            clock=clock,
        )
        observations.append((f"batch_{index + 1}", vectors, duration_ms))

    single_rows: list[np.ndarray] = []
    single_duration_ms = 0.0
    for item in canary_set.items:
        vectors, duration_ms = _embed_timed(
            provider,
            EmbeddingRequest(texts=(item.text,), purpose="document"),
            clock=clock,
        )
        single_rows.append(vectors[0])
        single_duration_ms += duration_ms
    single_vectors = np.ascontiguousarray(np.stack(single_rows, axis=0), dtype="<f4")
    observations.append(("single_order", single_vectors, single_duration_ms))

    checked: list[QualificationObservation] = []
    for observation_id, vectors, duration_ms in observations:
        checked_vectors = _validate_vectors(
            vectors,
            canary_count=len(canary_set.items),
            dimension=profile.dimension,
            reason_prefix=observation_id,
        )
        norms = np.linalg.norm(checked_vectors.astype(np.float64), axis=1)
        checked.append(
            QualificationObservation(
                observation_id=observation_id,
                duration_ms=duration_ms,
                norm_min=float(np.min(norms)),
                norm_max=float(np.max(norms)),
                result=compare_observation_to_baseline(
                    profile=profile,
                    canary_set=canary_set,
                    baseline=baseline,
                    observed=checked_vectors,
                ),
            )
        )

    return QualificationProbeResult(
        passed=all(observation.result.passed for observation in checked),
        profile_id=profile.profile_id,
        provider=profile.provider,
        model_id=profile.model_id,
        signature_fingerprint=profile.signature.fingerprint(),
        dimension=profile.dimension,
        canary_count=len(canary_set.items),
        logical_batch_size=len(canary_set.items),
        observations=tuple(checked),
    )


def capture_candidate(
    *,
    profile: EmbeddingSpaceProfile,
    canary_set: CanarySet,
    provider: EmbeddingProviderPort,
    output_dir: Path,
    thresholds: QualificationThresholds,
    repeat_count: int = 2,
    clock: Callable[[], float] = time.perf_counter,
) -> CandidateCaptureResult:
    """Capture a review-only baseline into a new, non-versioned candidate directory.

    This function never changes the selected profile, its manifest, or environment.
    Maintainers must inspect and promote candidate artifacts in a separate review.
    """

    if profile.release_status != "review":
        raise EmbeddingQualificationError("capture_requires_review_profile")
    _validate_thresholds(thresholds)
    if (
        canary_set.canary_set_version != profile.canary_set_version
        or not canary_set.items
    ):
        raise EmbeddingQualificationError("canary_set_invalid")
    if output_dir.exists():
        raise EmbeddingQualificationError("candidate_output_already_exists")
    if not _space_signature_matches(provider.signature, profile.signature):
        raise EmbeddingQualificationError("provider_signature_mismatch")

    texts = tuple(item.text for item in canary_set.items)
    baseline, baseline_duration_ms = _embed_timed(
        provider,
        EmbeddingRequest(texts=texts, purpose="document"),
        clock=clock,
    )
    baseline = _validate_vectors(
        baseline,
        canary_count=len(canary_set.items),
        dimension=profile.dimension,
        reason_prefix="candidate_baseline",
    )
    baseline_bytes = np.ascontiguousarray(baseline, dtype="<f4").tobytes(order="C")
    baseline_digest = f"sha256:{hashlib.sha256(baseline_bytes).hexdigest()}"
    proposed_artifact = (
        f"baselines/{profile.profile_id}_{profile.canary_set_version}.f32le"
    )
    candidate_profile = replace(
        profile,
        baseline_artifact=proposed_artifact,
        baseline_artifact_sha256=baseline_digest,
        qualification_thresholds=thresholds,
        expected_fingerprint=None,
    )
    expected_fingerprint = expected_qualification_fingerprint(
        candidate_profile,
        canary_count=len(canary_set.items),
    )
    candidate_profile = replace(
        candidate_profile,
        expected_fingerprint=expected_fingerprint,
    )

    # The capture itself is independently reported, followed by repeat/batch-order
    # checks.  No observation is averaged before comparison.
    baseline_norms = np.linalg.norm(baseline.astype(np.float64), axis=1)
    baseline_observation = QualificationObservation(
        observation_id="capture_batch",
        duration_ms=baseline_duration_ms,
        norm_min=float(np.min(baseline_norms)),
        norm_max=float(np.max(baseline_norms)),
        result=compare_observation_to_baseline(
            profile=candidate_profile,
            canary_set=canary_set,
            baseline=baseline,
            observed=baseline,
        ),
    )
    followup = run_qualification_probe(
        profile=candidate_profile,
        canary_set=canary_set,
        baseline=baseline,
        provider=provider,
        repeat_count=repeat_count,
        clock=clock,
        allow_unqualified_signature=True,
    )
    probe = replace(
        followup,
        passed=baseline_observation.result.passed and followup.passed,
        observations=(baseline_observation, *followup.observations),
    )

    report = probe_report_to_dict(probe)
    report.update(
        {
            "schema_version": "embedding-qualification-capture-report/v1",
            "command": "capture-candidate",
            "baseline_artifact_sha256": baseline_digest,
            "expected_fingerprint": expected_fingerprint,
        }
    )
    profile_patch = {
        "schema_version": "embedding-profile-patch-candidate/v1",
        "profile_id": profile.profile_id,
        "release_status": "review",
        "baseline_artifact": proposed_artifact,
        "baseline_artifact_sha256": baseline_digest,
        "fingerprint_algorithm": profile.fingerprint_algorithm,
        "expected_fingerprint": expected_fingerprint,
        "qualification_thresholds": asdict(thresholds),
        "canary_set_version": profile.canary_set_version,
        "canary_artifact_sha256": profile.canary_artifact_sha256,
    }

    output_dir.mkdir(parents=True, exist_ok=False)
    (output_dir / _CANDIDATE_BASELINE_NAME).write_bytes(baseline_bytes)
    _write_safe_json(output_dir / _CAPTURE_REPORT_NAME, report)
    _write_safe_json(output_dir / _PROFILE_PATCH_NAME, profile_patch)
    return CandidateCaptureResult(
        output_dir=output_dir,
        baseline_artifact_sha256=baseline_digest,
        expected_fingerprint=expected_fingerprint,
        probe=probe,
    )


def probe_report_to_dict(probe: QualificationProbeResult) -> dict[str, object]:
    """Create the allowlisted, coordinate-free representation used by the CLI."""

    return {
        "passed": probe.passed,
        "profile_id": probe.profile_id,
        "provider": probe.provider,
        "model_id": probe.model_id,
        "signature_fingerprint": probe.signature_fingerprint,
        "dimension": probe.dimension,
        "canary_count": probe.canary_count,
        "logical_batch_size": probe.logical_batch_size,
        "observations": [
            {
                "observation_id": observation.observation_id,
                "duration_ms": round(observation.duration_ms, 3),
                "norm_min": observation.norm_min,
                "norm_max": observation.norm_max,
                "passed": observation.result.passed,
                "expected_fingerprint": observation.result.expected_fingerprint,
                "observed_fingerprint": observation.result.observed_fingerprint,
                "metrics": [asdict(metric) for metric in observation.result.metrics],
            }
            for observation in probe.observations
        ],
    }


def _embed_timed(
    provider: EmbeddingProviderPort,
    request: EmbeddingRequest,
    *,
    clock: Callable[[], float],
) -> tuple[np.ndarray, float]:
    started_at = clock()
    result = provider.embed(request)
    duration_ms = max(0.0, (clock() - started_at) * 1000.0)
    if result.signature.canonical() != provider.signature.canonical():
        raise EmbeddingQualificationError("result_signature_mismatch")
    return result.vectors, duration_ms


def _space_signature_matches(left: object, right: object) -> bool:
    try:
        left_fields = asdict(left)  # type: ignore[arg-type]
        right_fields = asdict(right)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    # A review provider is necessarily created before a first baseline fingerprint
    # exists.  Capture may fill only that governance field; all vector-space fields
    # must already be identical.
    left_fields.pop("expected_fingerprint", None)
    right_fields.pop("expected_fingerprint", None)
    return left_fields == right_fields


def _validate_vectors(
    vectors: np.ndarray,
    *,
    canary_count: int,
    dimension: int,
    reason_prefix: str,
) -> np.ndarray:
    if canary_count <= 0:
        raise EmbeddingQualificationError(f"{reason_prefix}_empty")
    try:
        array = np.asarray(vectors)
    except Exception as exc:
        raise EmbeddingQualificationError(f"{reason_prefix}_not_array") from exc
    if array.dtype != np.dtype("<f4"):
        raise EmbeddingQualificationError(f"{reason_prefix}_dtype")
    if array.ndim != 2 or array.shape != (canary_count, dimension):
        raise EmbeddingQualificationError(f"{reason_prefix}_shape")
    if not array.flags.c_contiguous:
        raise EmbeddingQualificationError(f"{reason_prefix}_not_contiguous")
    if not np.all(np.isfinite(array)):
        raise EmbeddingQualificationError(f"{reason_prefix}_non_finite")
    norms = np.linalg.norm(array.astype(np.float64), axis=1)
    if np.any(~np.isfinite(norms)) or np.any(norms <= 0.0):
        raise EmbeddingQualificationError(f"{reason_prefix}_zero_norm")
    if np.any(np.abs(norms - 1.0) > _VECTOR_NORM_TOLERANCE):
        raise EmbeddingQualificationError(f"{reason_prefix}_not_normalized")
    return np.ascontiguousarray(array, dtype="<f4")


def _validate_thresholds(thresholds: QualificationThresholds) -> None:
    if not isinstance(thresholds, QualificationThresholds):
        raise EmbeddingQualificationError("candidate_thresholds_invalid")
    if (
        not math.isfinite(thresholds.min_cosine_per_vector)
        or not -1.0 <= thresholds.min_cosine_per_vector <= 1.0
        or not math.isfinite(thresholds.max_abs_coordinate_delta)
        or thresholds.max_abs_coordinate_delta < 0.0
    ):
        raise EmbeddingQualificationError("candidate_thresholds_invalid")


def _write_safe_json(path: Path, payload: dict[str, object]) -> None:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        allow_nan=False,
    )
    path.write_text(encoded + "\n", encoding="utf-8")
