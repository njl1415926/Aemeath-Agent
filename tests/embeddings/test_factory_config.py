from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from app.config import Settings
from app.embeddings.config import (
    load_embedding_space_profile,
    load_profile_for_qualification,
)
from app.embeddings.errors import EmbeddingConfigurationError
from app.embeddings.factory import create_embedding_provider
from app.embeddings.ports import EmbeddingProviderPort
from app.embeddings.schemas import (
    EmbeddingRequest,
    EmbeddingRuntimeOptions,
    QualificationThresholds,
)
from app.memory.runtime import build_memory_embedding_runtime

REVIEW_PROFILE_IDS = (
    "local_bge_m3_v1",
)

EMBEDDING_ENV_NAMES = (
    "AEMEATH_EMBEDDING_PROFILE",
    "AEMEATH_EMBEDDING_BATCH_SIZE",
    "AEMEATH_EMBEDDING_TIMEOUT_SECONDS",
    "AEMEATH_EMBEDDING_MAX_RETRIES",
    "AEMEATH_EMBEDDING_RETRY_BACKOFF_SECONDS",
    "AEMEATH_EMBEDDING_ALLOW_HASH",
    "AEMEATH_EMBEDDING_LOCAL_MODEL_PATH",
    "AEMEATH_EMBEDDING_LOCAL_DEVICE",
    "AEMEATH_EMBEDDING_LOCAL_MAX_LENGTH",
    "AEMEATH_EMBEDDING_LAZY_LOAD",
    "AEMEATH_MEMORY_EMBEDDING_PROVIDER",
    "AEMEATH_MEMORY_EMBEDDING_MODEL_PATH",
)


def _isolate_embedding_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.config.load_dotenv", lambda: None)
    for name in EMBEDDING_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("profile_id", REVIEW_PROFILE_IDS)
def test_review_profiles_are_qualification_only(profile_id: str) -> None:
    profile = load_profile_for_qualification(profile_id)

    assert profile.profile_id == profile_id
    assert profile.release_status == "review"

    with pytest.raises(EmbeddingConfigurationError) as loader_error:
        load_embedding_space_profile(profile_id)
    assert loader_error.value.reason == "profile_not_approved"

    with pytest.raises(EmbeddingConfigurationError) as factory_error:
        create_embedding_provider(
            profile,
            EmbeddingRuntimeOptions(allow_hash=True),
            secret_resolver=lambda _name: "unused-test-key",
        )
    assert factory_error.value.reason == "profile_not_approved"


def test_factory_hash_requires_explicit_opt_in_and_creates_independent_instances() -> None:
    approved = load_embedding_space_profile("deterministic_hash_v1")

    def unexpected_secret_lookup(_name: str) -> str:
        raise AssertionError("the deterministic Hash Provider must not resolve a secret")

    with pytest.raises(EmbeddingConfigurationError) as exc_info:
        create_embedding_provider(
            approved,
            EmbeddingRuntimeOptions(allow_hash=False),
            secret_resolver=unexpected_secret_lookup,
        )
    assert exc_info.value.reason == "hash_provider_not_explicitly_allowed"

    runtime = EmbeddingRuntimeOptions(allow_hash=True)
    first = create_embedding_provider(
        approved,
        runtime,
        secret_resolver=unexpected_secret_lookup,
    )
    second = create_embedding_provider(
        approved,
        runtime,
        secret_resolver=unexpected_secret_lookup,
    )

    assert isinstance(first.provider, EmbeddingProviderPort)
    assert isinstance(second.provider, EmbeddingProviderPort)
    assert first is not second
    request = EmbeddingRequest(texts=("factory order",), purpose="query")
    assert first.provider.embed(request).signature == approved.signature
    assert second.provider.embed(request).signature == approved.signature
    first.close()
    second.close()


def test_factory_accepts_canonical_approved_siliconflow_profile() -> None:
    approved = load_embedding_space_profile("siliconflow_pro_bge_m3_v1")
    requested_secret_names: list[str] = []

    def test_secret_lookup(name: str) -> str:
        requested_secret_names.append(name)
        return "test-only-siliconflow-key"

    provider_runtime = create_embedding_provider(
        approved,
        EmbeddingRuntimeOptions(allow_hash=False),
        secret_resolver=test_secret_lookup,
    )

    provider = provider_runtime.provider
    assert isinstance(provider, EmbeddingProviderPort)
    assert provider.signature == approved.signature
    assert provider.model_name == "Pro/BAAI/bge-m3"
    assert provider.dimension == 1024
    assert requested_secret_names == ["SILICONFLOW_API_KEY"]
    provider_runtime.close()


@pytest.mark.parametrize("profile_id", REVIEW_PROFILE_IDS)
def test_factory_rejects_forged_review_profile_before_provider_setup(
    profile_id: str,
) -> None:
    review = load_profile_for_qualification(profile_id)
    forged = replace(review, release_status="approved")

    def unexpected_secret_lookup(_name: str) -> str:
        raise AssertionError("a forged runtime profile reached Provider setup")

    with pytest.raises(EmbeddingConfigurationError) as exc_info:
        create_embedding_provider(
            forged,
            EmbeddingRuntimeOptions(allow_hash=True),
            secret_resolver=unexpected_secret_lookup,
        )

    assert exc_info.value.reason == "profile_not_approved"


@pytest.mark.parametrize(
    ("field_name", "tampered_value"),
    (
        ("schema_version", "embedding-space-profile/v999"),
        ("release_status", "review"),
        ("provider", "siliconflow"),
        ("model_id", "tampered-model"),
        ("dimension", 385),
        ("model_revision", "tampered-revision"),
        ("pooling", "tampered-pooling"),
        ("normalization", "tampered-normalization"),
        ("query_instruction", "tampered query"),
        ("document_instruction", "tampered document"),
        ("preprocessing_version", "tampered-pre-v1"),
        ("postprocessing_version", "tampered-post-v1"),
        ("canary_set_version", "tampered_canary_v1"),
        ("canary_artifact", "canaries/tampered.json"),
        ("canary_artifact_sha256", "sha256:" + "0" * 64),
        ("baseline_artifact", "baselines/tampered.f32le"),
        ("baseline_artifact_sha256", "sha256:" + "0" * 64),
        ("fingerprint_algorithm", "tampered-fingerprint-v1"),
        ("expected_fingerprint", "sha256:" + "0" * 64),
        (
            "qualification_thresholds",
            QualificationThresholds(
                min_cosine_per_vector=0.5,
                max_abs_coordinate_delta=0.5,
            ),
        ),
    ),
)
def test_factory_rejects_every_tampered_approved_hash_field(
    field_name: str,
    tampered_value: object,
) -> None:
    approved = load_embedding_space_profile("deterministic_hash_v1")
    tampered = replace(approved, **{field_name: tampered_value})

    with pytest.raises(EmbeddingConfigurationError) as exc_info:
        create_embedding_provider(
            tampered,
            EmbeddingRuntimeOptions(allow_hash=True),
            secret_resolver=lambda _name: "must-not-be-used",
        )

    assert exc_info.value.reason == "runtime_profile_not_canonical"


def test_settings_reads_canonical_embedding_environment_without_real_dotenv(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _isolate_embedding_environment(monkeypatch)
    model_path = tmp_path / "bge-m3"
    values = {
        "AEMEATH_EMBEDDING_PROFILE": "siliconflow_pro_bge_m3_v1",
        "AEMEATH_EMBEDDING_BATCH_SIZE": "7",
        "AEMEATH_EMBEDDING_TIMEOUT_SECONDS": "12.5",
        "AEMEATH_EMBEDDING_MAX_RETRIES": "4",
        "AEMEATH_EMBEDDING_RETRY_BACKOFF_SECONDS": "0.75",
        "AEMEATH_EMBEDDING_ALLOW_HASH": "true",
        "AEMEATH_EMBEDDING_LOCAL_MODEL_PATH": str(model_path),
        "AEMEATH_EMBEDDING_LOCAL_DEVICE": "cuda:2",
        "AEMEATH_EMBEDDING_LOCAL_MAX_LENGTH": "4096",
        "AEMEATH_EMBEDDING_LAZY_LOAD": "false",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)

    settings = Settings.from_env()

    assert settings.embedding_profile == "siliconflow_pro_bge_m3_v1"
    assert settings.embedding_batch_size == 7
    assert settings.embedding_timeout_seconds == 12.5
    assert settings.embedding_max_retries == 4
    assert settings.embedding_retry_backoff_seconds == 0.75
    assert settings.embedding_allow_hash is True
    assert settings.embedding_local_model_path == model_path
    assert settings.embedding_local_device == "cuda:2"
    assert settings.embedding_local_max_length == 4096
    assert settings.embedding_lazy_load is False
    assert settings.memory_embedding_provider is None


def test_settings_keeps_legacy_memory_embedding_environment_for_one_release(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _isolate_embedding_environment(monkeypatch)
    model_path = tmp_path / "legacy-bge-m3"
    monkeypatch.setenv("AEMEATH_MEMORY_EMBEDDING_PROVIDER", "bge-m3")
    monkeypatch.setenv("AEMEATH_MEMORY_EMBEDDING_MODEL_PATH", str(model_path))

    settings = Settings.from_env()

    assert settings.embedding_profile is None
    assert settings.memory_embedding_provider == "bge-m3"
    assert settings.memory_embedding_model_path == model_path


def test_settings_validation_rejects_invalid_canonical_runtime_option(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_embedding_environment(monkeypatch)
    monkeypatch.setenv("AEMEATH_EMBEDDING_BATCH_SIZE", "0")
    settings = Settings.from_env()

    with pytest.raises(EmbeddingConfigurationError) as exc_info:
        settings.validate()

    assert exc_info.value.reason == "runtime_batch_size_invalid"


def test_settings_rejects_non_numeric_embedding_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_embedding_environment(monkeypatch)
    monkeypatch.setenv("AEMEATH_EMBEDDING_BATCH_SIZE", "not-an-integer")

    with pytest.raises(EmbeddingConfigurationError) as exc_info:
        Settings.from_env()

    assert exc_info.value.reason == "embedding_integer_environment_invalid"


def test_memory_runtime_keeps_explicit_legacy_hash_for_compatibility(settings) -> None:
    provider_runtime = build_memory_embedding_runtime(settings)
    provider = provider_runtime.provider

    assert provider.model_name == "deterministic_hash_v1"
    assert provider.dimension == 384
    provider_runtime.close()


def test_memory_runtime_rejects_conflicting_canonical_and_legacy_selection(settings) -> None:
    configured = replace(
        settings,
        embedding_profile="siliconflow_pro_bge_m3_v1",
        memory_embedding_provider="hash",
    )

    with pytest.raises(EmbeddingConfigurationError) as exc_info:
        build_memory_embedding_runtime(configured)

    assert exc_info.value.reason == "canonical_legacy_profile_conflict"


def test_memory_runtime_does_not_create_implicit_hash_when_unconfigured(settings) -> None:
    configured = replace(
        settings,
        embedding_profile=None,
        memory_embedding_provider=None,
    )

    with pytest.raises(EmbeddingConfigurationError) as exc_info:
        build_memory_embedding_runtime(configured)

    assert exc_info.value.reason == "embedding_profile_not_configured"


def test_memory_runtime_legacy_bge_alias_cannot_bypass_approved_gate(settings) -> None:
    configured = replace(
        settings,
        embedding_profile=None,
        memory_embedding_provider="bge-m3",
    )

    with pytest.raises(EmbeddingConfigurationError) as exc_info:
        build_memory_embedding_runtime(configured)

    assert exc_info.value.reason == "profile_not_approved"
