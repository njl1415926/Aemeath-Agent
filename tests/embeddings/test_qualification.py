from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from app.embeddings.errors import EmbeddingQualificationError
from app.embeddings.fingerprints import expected_qualification_fingerprint
from app.embeddings.qualification import (
    capture_candidate,
    compare_observation_to_baseline,
    run_qualification_probe,
)
from app.embeddings.schemas import (
    CanaryItem,
    CanarySet,
    EmbeddingBatchResult,
    EmbeddingRequest,
    EmbeddingSpaceProfile,
    EmbeddingUsage,
    QualificationThresholds,
)


def _normalized(rows: list[list[float]]) -> np.ndarray:
    array = np.asarray(rows, dtype=np.float64)
    array /= np.linalg.norm(array, axis=1, keepdims=True)
    return np.ascontiguousarray(array, dtype="<f4")


def _canaries() -> CanarySet:
    return CanarySet(
        schema_version="embedding-canary-set/v1",
        canary_set_version="test_canary_v1",
        items=(
            CanaryItem(canary_id="first", text="fixed first canary"),
            CanaryItem(canary_id="second", text="fixed second canary"),
        ),
    )


def _review_profile() -> EmbeddingSpaceProfile:
    return EmbeddingSpaceProfile(
        schema_version="embedding-space-profile/v1",
        release_status="review",
        profile_id="test_profile_v1",
        provider="siliconflow",
        model_id="test-model",
        dimension=3,
        model_revision="provider_managed",
        pooling="provider_managed",
        normalization="l2",
        query_instruction="",
        document_instruction="",
        preprocessing_version="test-pre-v1",
        postprocessing_version="normalized-float32-v1",
        canary_set_version="test_canary_v1",
        canary_artifact="canaries/test.json",
        canary_artifact_sha256="sha256:" + "1" * 64,
        baseline_artifact=None,
        baseline_artifact_sha256=None,
        fingerprint_algorithm="baseline-relative-mask-v1",
        expected_fingerprint=None,
        qualification_thresholds=None,
    )


def _qualified_profile(
    baseline: np.ndarray,
    *,
    max_delta: float = 0.01,
    min_cosine: float = 0.999,
) -> EmbeddingSpaceProfile:
    raw = np.ascontiguousarray(baseline, dtype="<f4").tobytes(order="C")
    profile = replace(
        _review_profile(),
        baseline_artifact="baselines/test.f32le",
        baseline_artifact_sha256=f"sha256:{hashlib.sha256(raw).hexdigest()}",
        qualification_thresholds=QualificationThresholds(
            min_cosine_per_vector=min_cosine,
            max_abs_coordinate_delta=max_delta,
        ),
    )
    return replace(
        profile,
        expected_fingerprint=expected_qualification_fingerprint(profile, canary_count=2),
    )


class _FakeProvider:
    def __init__(self, profile: EmbeddingSpaceProfile, vectors: np.ndarray):
        self._signature = profile.signature
        self._vectors = vectors
        self.requests: list[EmbeddingRequest] = []
        self._by_text = {
            "fixed first canary": vectors[0],
            "fixed second canary": vectors[1],
        }

    @property
    def signature(self):
        return self._signature

    def embed(self, request: EmbeddingRequest) -> EmbeddingBatchResult:
        self.requests.append(request)
        vectors = np.stack([self._by_text[text] for text in request.texts], axis=0)
        return EmbeddingBatchResult(
            vectors=np.ascontiguousarray(vectors, dtype="<f4"),
            signature=self.signature,
            usage=EmbeddingUsage(input_count=len(request.texts)),
        )


def test_threshold_noise_keeps_expected_fingerprint() -> None:
    baseline = _normalized([[0.6, 0.8, 0.0], [0.0, 0.6, 0.8]])
    profile = _qualified_profile(baseline, max_delta=0.01, min_cosine=0.999)
    observed = baseline.astype(np.float64)
    observed[0, 0] += 0.001
    observed[1, 2] -= 0.001
    observed /= np.linalg.norm(observed, axis=1, keepdims=True)
    observed = np.ascontiguousarray(observed, dtype="<f4")

    result = compare_observation_to_baseline(
        profile=profile,
        canary_set=_canaries(),
        baseline=baseline,
        observed=observed,
    )

    assert result.passed is True
    assert result.observed_fingerprint == result.expected_fingerprint


def test_one_coordinate_over_threshold_changes_fingerprint() -> None:
    baseline = _normalized([[0.6, 0.8, 0.0], [0.0, 0.6, 0.8]])
    profile = _qualified_profile(baseline, max_delta=0.001, min_cosine=-1.0)
    observed = baseline.astype(np.float64)
    observed[0, 2] = 0.02
    observed /= np.linalg.norm(observed, axis=1, keepdims=True)
    observed = np.ascontiguousarray(observed, dtype="<f4")

    result = compare_observation_to_baseline(
        profile=profile,
        canary_set=_canaries(),
        baseline=baseline,
        observed=observed,
    )

    assert result.passed is False
    assert result.observed_fingerprint != result.expected_fingerprint


def test_batch_row_permutation_is_rejected() -> None:
    baseline = _normalized([[0.6, 0.8, 0.0], [0.0, 0.6, 0.8]])
    profile = _qualified_profile(baseline, max_delta=0.001, min_cosine=-1.0)
    observed = np.ascontiguousarray(baseline[::-1], dtype="<f4")

    result = compare_observation_to_baseline(
        profile=profile,
        canary_set=_canaries(),
        baseline=baseline,
        observed=observed,
    )

    assert result.passed is False
    assert [metric.canary_id for metric in result.metrics] == ["first", "second"]


def test_common_orthogonal_rotation_preserves_internal_cosines_but_fails() -> None:
    baseline = _normalized([[0.6, 0.8, 0.0], [0.0, 0.6, 0.8]])
    rotation = np.asarray(
        [[0.0, -1.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, 1.0]],
        dtype=np.float64,
    )
    observed = np.ascontiguousarray(baseline.astype(np.float64) @ rotation, dtype="<f4")
    profile = _qualified_profile(baseline, max_delta=0.001, min_cosine=-1.0)

    baseline_gram = baseline.astype(np.float64) @ baseline.astype(np.float64).T
    observed_gram = observed.astype(np.float64) @ observed.astype(np.float64).T
    result = compare_observation_to_baseline(
        profile=profile,
        canary_set=_canaries(),
        baseline=baseline,
        observed=observed,
    )

    np.testing.assert_allclose(observed_gram, baseline_gram, atol=1e-7)
    assert result.passed is False
    assert result.observed_fingerprint != result.expected_fingerprint


def test_probe_checks_repeated_batch_and_individual_order() -> None:
    baseline = _normalized([[0.6, 0.8, 0.0], [0.0, 0.6, 0.8]])
    profile = _qualified_profile(baseline, max_delta=0.001)
    provider = _FakeProvider(profile, baseline)

    result = run_qualification_probe(
        profile=profile,
        canary_set=_canaries(),
        baseline=baseline,
        provider=provider,
        repeat_count=2,
    )

    assert result.passed is True
    assert [item.observation_id for item in result.observations] == [
        "batch_1",
        "batch_2",
        "single_order",
    ]
    assert [len(request.texts) for request in provider.requests] == [2, 2, 1, 1]


def test_candidate_capture_writes_only_new_review_artifacts(tmp_path: Path) -> None:
    baseline = _normalized([[0.6, 0.8, 0.0], [0.0, 0.6, 0.8]])
    profile = _review_profile()
    provider = _FakeProvider(profile, baseline)
    output_dir = tmp_path / "new-candidate"

    result = capture_candidate(
        profile=profile,
        canary_set=_canaries(),
        provider=provider,
        output_dir=output_dir,
        thresholds=QualificationThresholds(
            min_cosine_per_vector=0.999,
            max_abs_coordinate_delta=0.001,
        ),
    )

    assert result.probe.passed is True
    assert sorted(path.name for path in output_dir.iterdir()) == [
        "baseline.candidate.f32le",
        "capture_report.json",
        "profile_patch.candidate.json",
    ]
    assert (output_dir / "baseline.candidate.f32le").read_bytes() == baseline.tobytes()
    report_text = (output_dir / "capture_report.json").read_text(encoding="utf-8")
    report = json.loads(report_text)
    assert report["passed"] is True
    assert not any(
        forbidden in report_text.lower()
        for forbidden in ("api_key", "authorization", '"text"', '"vectors"', '"embedding"')
    )
    patch = json.loads(
        (output_dir / "profile_patch.candidate.json").read_text(encoding="utf-8")
    )
    assert patch["release_status"] == "review"
    assert patch["expected_fingerprint"] == result.expected_fingerprint


def test_candidate_capture_refuses_to_overwrite_directory(tmp_path: Path) -> None:
    baseline = _normalized([[0.6, 0.8, 0.0], [0.0, 0.6, 0.8]])
    provider = _FakeProvider(_review_profile(), baseline)
    output_dir = tmp_path / "existing"
    output_dir.mkdir()
    marker = output_dir / "keep.txt"
    marker.write_text("keep", encoding="utf-8")

    with pytest.raises(EmbeddingQualificationError) as error:
        capture_candidate(
            profile=_review_profile(),
            canary_set=_canaries(),
            provider=provider,
            output_dir=output_dir,
            thresholds=QualificationThresholds(0.999, 0.001),
        )

    assert error.value.reason == "candidate_output_already_exists"
    assert marker.read_text(encoding="utf-8") == "keep"
    assert provider.requests == []
