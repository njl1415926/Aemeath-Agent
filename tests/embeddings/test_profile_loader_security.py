from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from app.embeddings import config
from app.embeddings.errors import EmbeddingConfigurationError


def _hash_profile_data() -> dict[str, object]:
    raw = config._profile_root().joinpath("deterministic_hash_v1.json").read_bytes()
    return json.loads(raw.decode("utf-8"))


def _parse_profile(data: dict[str, object]):
    return config._parse_profile(
        json.dumps(data, ensure_ascii=False).encode("utf-8"),
        "deterministic_hash_v1",
    )


def _qualified_profile_data(*, release_status: str) -> dict[str, object]:
    data = _hash_profile_data()
    data.update(
        {
            "release_status": release_status,
            "baseline_artifact": "baselines/deterministic_hash_v1.f32le",
            "baseline_artifact_sha256": "sha256:" + "1" * 64,
            "expected_fingerprint": "sha256:" + "2" * 64,
            "qualification_thresholds": {
                "min_cosine_per_vector": 0.999,
                "max_abs_coordinate_delta": 0.001,
            },
        }
    )
    return data


def _unqualified_review_profile_data() -> dict[str, object]:
    data = _hash_profile_data()
    data.update(
        {
            "release_status": "review",
            "baseline_artifact": None,
            "baseline_artifact_sha256": None,
            "expected_fingerprint": None,
            "qualification_thresholds": None,
        }
    )
    return data


@pytest.mark.parametrize(
    "profile_id",
    ("../escape", "canaries/escape", "/absolute", "profile.json", "UPPER_CASE"),
)
def test_profile_id_rejects_traversal_before_resource_lookup(
    profile_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_resource_lookup():
        raise AssertionError("invalid profile id reached package resource lookup")

    monkeypatch.setattr(config, "_profile_root", unexpected_resource_lookup)

    with pytest.raises(EmbeddingConfigurationError) as exc_info:
        config.load_profile_for_qualification(profile_id)

    assert exc_info.value.reason == "profile_id_invalid"


@pytest.mark.parametrize(
    ("field", "value", "expected_reason"),
    (
        ("canary_artifact", "../outside.json", "canary_artifact_invalid"),
        ("canary_artifact", "canaries/../outside.json", "canary_artifact_invalid"),
        ("canary_artifact", r"canaries\outside.json", "canary_artifact_invalid"),
        ("baseline_artifact", "baselines/../../outside.f32le", "baseline_artifact_invalid"),
        ("baseline_artifact", "/baselines/outside.f32le", "baseline_artifact_invalid"),
    ),
)
def test_profile_artifact_paths_cannot_escape_allowed_roots(
    field: str,
    value: str,
    expected_reason: str,
) -> None:
    data = _qualified_profile_data(release_status="review")
    data[field] = value

    with pytest.raises(EmbeddingConfigurationError) as exc_info:
        _parse_profile(data)

    assert exc_info.value.reason == expected_reason


def test_profile_json_rejects_duplicate_and_unknown_keys() -> None:
    data = _hash_profile_data()
    canonical = json.dumps(data, ensure_ascii=False)
    duplicate = canonical[:-1] + ', "profile_id": "deterministic_hash_v1"}'

    with pytest.raises(EmbeddingConfigurationError) as duplicate_error:
        config._parse_profile(duplicate.encode("utf-8"), "deterministic_hash_v1")
    assert duplicate_error.value.reason == "profile_json_duplicate_key"

    data["unexpected_field"] = "must not be ignored"
    with pytest.raises(EmbeddingConfigurationError) as unknown_error:
        _parse_profile(data)
    assert unknown_error.value.reason == "profile_fields_invalid"


def test_canary_json_rejects_unknown_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    profile = config.load_profile_for_qualification("deterministic_hash_v1")
    canary = {
        "schema_version": "embedding-canary-set/v1",
        "canary_set_version": profile.canary_set_version,
        "canaries": [{"id": "safe", "text": "fixed safe text", "extra": True}],
    }
    monkeypatch.setattr(
        config,
        "read_profile_artifact",
        lambda *_args, **_kwargs: json.dumps(canary).encode("utf-8"),
    )

    with pytest.raises(EmbeddingConfigurationError) as exc_info:
        config.load_canary_set(profile)

    assert exc_info.value.reason == "canary_item_fields_invalid"


def test_review_profile_may_omit_qualification_but_partial_fields_are_rejected() -> None:
    review = _parse_profile(_unqualified_review_profile_data())
    assert review.release_status == "review"
    assert review.baseline_artifact is None
    assert review.qualification_thresholds is None

    partial = _unqualified_review_profile_data()
    partial["baseline_artifact"] = "baselines/incomplete.f32le"
    with pytest.raises(EmbeddingConfigurationError) as exc_info:
        _parse_profile(partial)
    assert exc_info.value.reason == "qualification_fields_incomplete"


def test_approved_profile_requires_complete_qualification_fields() -> None:
    incomplete = _unqualified_review_profile_data()
    incomplete["release_status"] = "approved"

    with pytest.raises(EmbeddingConfigurationError) as exc_info:
        _parse_profile(incomplete)

    assert exc_info.value.reason == "approved_profile_not_qualified"

    approved = _parse_profile(_qualified_profile_data(release_status="approved"))
    assert approved.release_status == "approved"
    assert approved.baseline_artifact is not None
    assert approved.expected_fingerprint is not None
    assert approved.qualification_thresholds is not None


def test_tampered_canary_digest_is_rejected() -> None:
    profile = config.load_profile_for_qualification("deterministic_hash_v1")
    tampered = replace(profile, canary_artifact_sha256="sha256:" + "0" * 64)

    with pytest.raises(EmbeddingConfigurationError) as exc_info:
        config.load_canary_set(tampered)

    assert exc_info.value.reason == "profile_artifact_digest_mismatch"


def _profile_with_baseline():
    profile = config.load_profile_for_qualification("deterministic_hash_v1")
    return replace(
        profile,
        baseline_artifact="baselines/test.f32le",
        baseline_artifact_sha256="sha256:" + "0" * 64,
    )


def test_baseline_rejects_wrong_byte_length(monkeypatch: pytest.MonkeyPatch) -> None:
    profile = _profile_with_baseline()
    monkeypatch.setattr(
        config,
        "read_profile_artifact",
        lambda *_args, **_kwargs: np.zeros(profile.dimension - 1, dtype="<f4").tobytes(),
    )

    with pytest.raises(EmbeddingConfigurationError) as exc_info:
        config.load_baseline(profile, canary_count=1)

    assert exc_info.value.reason == "baseline_byte_length_mismatch"


@pytest.mark.parametrize(
    ("first_coordinate", "expected_reason"),
    ((float("nan"), "baseline_non_finite"), (2.0, "baseline_not_normalized")),
)
def test_baseline_rejects_non_finite_or_non_normalized_rows(
    first_coordinate: float,
    expected_reason: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _profile_with_baseline()
    vector = np.zeros(profile.dimension, dtype="<f4")
    vector[0] = first_coordinate
    monkeypatch.setattr(
        config,
        "read_profile_artifact",
        lambda *_args, **_kwargs: vector.tobytes(order="C"),
    )

    with pytest.raises(EmbeddingConfigurationError) as exc_info:
        config.load_baseline(profile, canary_count=1)

    assert exc_info.value.reason == expected_reason


def test_loader_recomputes_and_rejects_mismatched_expected_fingerprint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canary = {
        "schema_version": "embedding-canary-set/v1",
        "canary_set_version": "hash_canary_v1",
        "canaries": [{"id": "fixed", "text": "fixed non-sensitive canary"}],
    }
    canary_raw = json.dumps(canary, separators=(",", ":")).encode("utf-8")
    baseline = np.zeros(384, dtype="<f4")
    baseline[0] = 1.0
    baseline_raw = baseline.tobytes(order="C")
    data = _qualified_profile_data(release_status="approved")
    data.update(
        {
            "canary_artifact": "canaries/fixed.json",
            "canary_artifact_sha256": (
                f"sha256:{hashlib.sha256(canary_raw).hexdigest()}"
            ),
            "baseline_artifact": "baselines/fixed.f32le",
            "baseline_artifact_sha256": (
                f"sha256:{hashlib.sha256(baseline_raw).hexdigest()}"
            ),
            "expected_fingerprint": "sha256:" + "f" * 64,
        }
    )
    (tmp_path / "canaries").mkdir()
    (tmp_path / "baselines").mkdir()
    (tmp_path / "canaries" / "fixed.json").write_bytes(canary_raw)
    (tmp_path / "baselines" / "fixed.f32le").write_bytes(baseline_raw)
    (tmp_path / "deterministic_hash_v1.json").write_text(
        json.dumps(data),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "_profile_root", lambda: tmp_path)

    with pytest.raises(EmbeddingConfigurationError) as exc_info:
        config.load_profile_for_qualification("deterministic_hash_v1")

    assert exc_info.value.reason == "expected_fingerprint_mismatch"
