from __future__ import annotations

import hashlib

import pytest

from app.embeddings.config import (
    load_baseline,
    load_canary_set,
    load_embedding_space_profile,
    load_profile_for_qualification,
    read_profile_artifact,
)
from app.embeddings.errors import EmbeddingConfigurationError
from app.embeddings.factory import create_embedding_provider
from app.embeddings.qualification import run_qualification_probe
from app.embeddings.schemas import EmbeddingRequest, EmbeddingRuntimeOptions


def _unexpected_secret_lookup(_name: str) -> str:
    raise AssertionError("the deterministic Hash Provider must remain fully offline")


def test_approved_hash_baseline_recomputes_byte_for_byte_through_factory() -> None:
    profile = load_embedding_space_profile("deterministic_hash_v1")
    canary_set = load_canary_set(profile)
    baseline = load_baseline(profile, canary_count=len(canary_set.items))
    assert profile.release_status == "approved"
    assert profile.baseline_artifact is not None
    assert profile.baseline_artifact_sha256 is not None
    assert profile.expected_fingerprint is not None

    baseline_bytes = read_profile_artifact(
        profile.baseline_artifact,
        profile.baseline_artifact_sha256,
        allowed_root="baselines",
    )
    assert baseline.tobytes(order="C") == baseline_bytes
    assert (
        f"sha256:{hashlib.sha256(baseline_bytes).hexdigest()}"
        == profile.baseline_artifact_sha256
    )

    provider = create_embedding_provider(
        profile,
        EmbeddingRuntimeOptions(allow_hash=True, batch_size=2),
        secret_resolver=_unexpected_secret_lookup,
    )
    result = provider.embed(
        EmbeddingRequest(
            texts=tuple(item.text for item in canary_set.items),
            purpose="document",
        )
    )

    assert result.signature == profile.signature
    assert result.vectors.tobytes(order="C") == baseline_bytes


def test_approved_hash_profile_passes_offline_qualification() -> None:
    profile = load_embedding_space_profile("deterministic_hash_v1")
    canary_set = load_canary_set(profile)
    baseline = load_baseline(profile, canary_count=len(canary_set.items))
    provider = create_embedding_provider(
        profile,
        EmbeddingRuntimeOptions(allow_hash=True, batch_size=2),
        secret_resolver=_unexpected_secret_lookup,
    )

    probe = run_qualification_probe(
        profile=profile,
        canary_set=canary_set,
        baseline=baseline,
        provider=provider,
        repeat_count=2,
    )

    assert probe.passed is True
    assert [observation.result.passed for observation in probe.observations] == [
        True,
        True,
        True,
    ]
    assert all(
        observation.result.expected_fingerprint
        == observation.result.observed_fingerprint
        == profile.expected_fingerprint
        for observation in probe.observations
    )


def test_local_bge_profile_remains_unqualified_review_only() -> None:
    profile_id = "local_bge_m3_v1"
    profile = load_profile_for_qualification(profile_id)

    assert profile.release_status == "review"
    assert profile.baseline_artifact is None
    assert profile.baseline_artifact_sha256 is None
    assert profile.expected_fingerprint is None
    assert profile.qualification_thresholds is None
    with pytest.raises(EmbeddingConfigurationError) as exc_info:
        load_embedding_space_profile(profile_id)
    assert exc_info.value.reason == "profile_not_approved"


def test_approved_siliconflow_profile_loads_versioned_baseline() -> None:
    profile = load_embedding_space_profile("siliconflow_pro_bge_m3_v1")
    canary_set = load_canary_set(profile)
    baseline = load_baseline(profile, canary_count=len(canary_set.items))

    assert profile.release_status == "approved"
    assert profile.baseline_artifact is not None
    assert profile.baseline_artifact_sha256 is not None
    assert profile.expected_fingerprint is not None
    assert profile.qualification_thresholds is not None
    assert profile.expected_fingerprint == (
        "sha256:9117a6ed90373361c1374c4b87e065c451ac4e38e9ee3abc8735317b70b1d64e"
    )
    assert profile.qualification_thresholds.min_cosine_per_vector == 0.9999
    assert profile.qualification_thresholds.max_abs_coordinate_delta == 0.003
    assert baseline.shape == (len(canary_set.items), 1024)
    assert baseline.dtype.name == "float32"

    baseline_bytes = read_profile_artifact(
        profile.baseline_artifact,
        profile.baseline_artifact_sha256,
        allowed_root="baselines",
    )
    assert baseline.tobytes(order="C") == baseline_bytes
    assert (
        f"sha256:{hashlib.sha256(baseline_bytes).hexdigest()}"
        == profile.baseline_artifact_sha256
    )


@pytest.mark.parametrize(
    "profile_id",
    ("siliconflow_pro_bge_m3_v1", "local_bge_m3_v1"),
)
def test_bge_canary_artifact_has_canonical_text_format(profile_id: str) -> None:
    profile = load_profile_for_qualification(profile_id)
    raw = read_profile_artifact(
        profile.canary_artifact,
        profile.canary_artifact_sha256,
        allowed_root="canaries",
    )

    assert not raw.startswith(b"\xef\xbb\xbf")
    assert b"\r" not in raw
    assert raw.endswith(b"\n")
    assert not raw.endswith(b"\n\n")
