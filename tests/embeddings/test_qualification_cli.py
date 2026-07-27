from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace
from pathlib import Path

import numpy as np
import pytest

from app.embeddings import qualify
from app.embeddings.errors import EmbeddingQualificationError
from app.embeddings.fingerprints import expected_qualification_fingerprint
from app.embeddings.schemas import (
    CanaryItem,
    CanarySet,
    EmbeddingBatchResult,
    EmbeddingRequest,
    EmbeddingSpaceProfile,
    QualificationThresholds,
)
from app.observability.schemas import AppLogRecord

SENTINEL = "NEVER_LOG_THIS_QUALIFICATION_SECRET"
QUALIFICATION_PAYLOAD_FIELDS = {
    "command",
    "provider",
    "profile_id",
    "release_status",
    "dimension",
    "canary_count",
    "observation_count",
    "signature_fingerprint",
    "duration_ms",
}


@pytest.fixture(autouse=True)
def _isolate_project_dotenv(monkeypatch) -> None:
    monkeypatch.setattr(qualify, "load_dotenv", lambda: False)
    monkeypatch.setenv("AEMEATH_ENABLE_APP_LOG", "false")


def _vectors() -> np.ndarray:
    return np.ascontiguousarray([[1.0, 0.0], [0.0, 1.0]], dtype="<f4")


def _canaries() -> CanarySet:
    return CanarySet(
        schema_version="embedding-canary-set/v1",
        canary_set_version="cli_canary_v1",
        items=(
            CanaryItem(canary_id="one", text="safe canary one"),
            CanaryItem(canary_id="two", text="safe canary two"),
        ),
    )


def _review_profile(*, with_baseline: bool) -> EmbeddingSpaceProfile:
    profile = EmbeddingSpaceProfile(
        schema_version="embedding-space-profile/v1",
        release_status="review",
        profile_id="cli_profile_v1",
        provider="siliconflow",
        model_id="test-model",
        dimension=2,
        model_revision="provider_managed",
        pooling="provider_managed",
        normalization="l2",
        query_instruction="",
        document_instruction="",
        preprocessing_version="test-pre-v1",
        postprocessing_version="normalized-float32-v1",
        canary_set_version="cli_canary_v1",
        canary_artifact="canaries/cli.json",
        canary_artifact_sha256="sha256:" + "1" * 64,
        baseline_artifact=None,
        baseline_artifact_sha256=None,
        fingerprint_algorithm="baseline-relative-mask-v1",
        expected_fingerprint=None,
        qualification_thresholds=None,
    )
    if not with_baseline:
        return profile
    raw = _vectors().tobytes()
    profile = replace(
        profile,
        baseline_artifact="baselines/cli.f32le",
        baseline_artifact_sha256=f"sha256:{hashlib.sha256(raw).hexdigest()}",
        qualification_thresholds=QualificationThresholds(0.9999, 0.0001),
    )
    return replace(
        profile,
        expected_fingerprint=expected_qualification_fingerprint(profile, canary_count=2),
    )


class _FakeProvider:
    def __init__(self, profile: EmbeddingSpaceProfile):
        self._signature = profile.signature
        self.requests: list[EmbeddingRequest] = []
        self._rows = {
            "safe canary one": _vectors()[0],
            "safe canary two": _vectors()[1],
        }

    @property
    def signature(self):
        return self._signature

    def embed(self, request: EmbeddingRequest) -> EmbeddingBatchResult:
        self.requests.append(request)
        return EmbeddingBatchResult(
            vectors=np.ascontiguousarray(
                np.stack([self._rows[text] for text in request.texts]),
                dtype="<f4",
            ),
            signature=self.signature,
        )


class _MismatchedProvider(_FakeProvider):
    def __init__(self, profile: EmbeddingSpaceProfile):
        super().__init__(profile)
        self._rows = {
            "safe canary one": _vectors()[1],
            "safe canary two": _vectors()[0],
        }


class _CollectingLogger:
    def __init__(self) -> None:
        self.records: list[AppLogRecord] = []

    def record(self, record: AppLogRecord) -> None:
        self.records.append(record)


def _qualification_records(logger: _CollectingLogger) -> list[AppLogRecord]:
    return [
        record
        for record in logger.records
        if record.event_name.startswith("embedding.qualification_")
    ]


def _serialized(record: AppLogRecord) -> str:
    return json.dumps(asdict(record), ensure_ascii=False, sort_keys=True)


def test_capture_candidate_cli_writes_safe_files_without_network(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    profile = _review_profile(with_baseline=False)
    provider = _FakeProvider(profile)
    output_dir = tmp_path / "candidate"
    monkeypatch.setenv("SILICONFLOW_API_KEY", "must-never-appear")
    monkeypatch.setattr(qualify, "load_profile_for_qualification", lambda profile_id: profile)
    monkeypatch.setattr(qualify, "load_canary_set", lambda selected: _canaries())
    monkeypatch.setattr(
        qualify,
        "create_qualification_provider",
        lambda selected, runtime, *, app_logger=None: provider,
    )

    exit_code = qualify.main(
        [
            "capture-candidate",
            "--profile",
            profile.profile_id,
            "--output-dir",
            str(output_dir),
            "--min-cosine-per-vector",
            "0.9999",
            "--max-abs-coordinate-delta",
            "0.0001",
        ]
    )

    stdout = capsys.readouterr().out
    payload = json.loads(stdout)
    assert exit_code == 0
    assert payload["passed"] is True
    assert "must-never-appear" not in stdout
    assert (output_dir / "baseline.candidate.f32le").exists()
    assert (output_dir / "capture_report.json").exists()
    assert (output_dir / "profile_patch.candidate.json").exists()


def test_verify_cli_accepts_review_profile_with_committed_baseline(
    monkeypatch,
    capsys,
) -> None:
    profile = _review_profile(with_baseline=True)
    provider = _FakeProvider(profile)
    monkeypatch.setattr(qualify, "load_profile_for_qualification", lambda profile_id: profile)
    monkeypatch.setattr(qualify, "load_canary_set", lambda selected: _canaries())
    monkeypatch.setattr(
        qualify,
        "load_baseline",
        lambda selected, canary_count: _vectors(),
    )
    monkeypatch.setattr(
        qualify,
        "create_qualification_provider",
        lambda selected, runtime, *, app_logger=None: provider,
    )

    exit_code = qualify.main(["verify", "--profile", profile.profile_id])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["passed"] is True
    assert payload["command"] == "verify"
    assert payload["error_code"] is None
    assert [item["observation_id"] for item in payload["observations"]] == [
        "batch_1",
        "batch_2",
        "single_order",
    ]


def test_verify_cli_fails_safely_before_provider_when_baseline_is_missing(
    monkeypatch,
    capsys,
) -> None:
    profile = _review_profile(with_baseline=False)
    provider_created = False

    def fail_baseline(selected, canary_count):
        raise EmbeddingQualificationError("profile_baseline_missing")

    def create_provider(selected, runtime, *, app_logger=None):
        nonlocal provider_created
        provider_created = True
        raise AssertionError("must not create provider")

    monkeypatch.setattr(qualify, "load_profile_for_qualification", lambda profile_id: profile)
    monkeypatch.setattr(qualify, "load_canary_set", lambda selected: _canaries())
    monkeypatch.setattr(qualify, "load_baseline", fail_baseline)
    monkeypatch.setattr(qualify, "create_qualification_provider", create_provider)

    exit_code = qualify.main(["verify", "--profile", profile.profile_id])

    stdout = capsys.readouterr().out
    payload = json.loads(stdout)
    assert exit_code == 2
    assert provider_created is False
    assert payload == {
        "command": "verify",
        "error_code": "EMBEDDING_QUALIFICATION_FAILED",
        "passed": False,
        "profile_id": profile.profile_id,
        "retryable": False,
    }


def test_capture_cli_refuses_existing_directory_before_provider_creation(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    profile = _review_profile(with_baseline=False)
    output_dir = tmp_path / "existing"
    output_dir.mkdir()
    provider_created = False

    def create_provider(selected, runtime, *, app_logger=None):
        nonlocal provider_created
        provider_created = True
        raise AssertionError("must not create provider")

    monkeypatch.setattr(qualify, "load_profile_for_qualification", lambda profile_id: profile)
    monkeypatch.setattr(qualify, "load_canary_set", lambda selected: _canaries())
    monkeypatch.setattr(qualify, "create_qualification_provider", create_provider)

    exit_code = qualify.main(
        [
            "capture-candidate",
            "--profile",
            profile.profile_id,
            "--output-dir",
            str(output_dir),
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert provider_created is False
    assert payload["error_code"] == "EMBEDDING_QUALIFICATION_FAILED"


def test_verify_records_one_completed_terminal_with_exact_safe_payload(
    monkeypatch,
    capsys,
) -> None:
    profile = _review_profile(with_baseline=True)
    provider = _FakeProvider(profile)
    logger = _CollectingLogger()
    factory_loggers: list[object] = []

    def create_provider(selected, runtime, *, app_logger=None):
        factory_loggers.append(app_logger)
        return provider

    monkeypatch.setenv("SILICONFLOW_API_KEY", SENTINEL)
    monkeypatch.setattr(qualify, "load_profile_for_qualification", lambda profile_id: profile)
    monkeypatch.setattr(qualify, "load_canary_set", lambda selected: _canaries())
    monkeypatch.setattr(
        qualify,
        "load_baseline",
        lambda selected, canary_count: _vectors(),
    )
    monkeypatch.setattr(qualify, "create_qualification_provider", create_provider)
    ticks = iter((10.0, 10.125))

    exit_code = qualify.main(
        ["verify", "--profile", profile.profile_id],
        app_logger=logger,
        clock=lambda: next(ticks),
    )

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)["passed"] is True
    assert factory_loggers == [logger]
    records = _qualification_records(logger)
    assert len(records) == 1
    record = records[0]
    assert record.event_name == "embedding.qualification_completed"
    assert record.level == "info"
    assert record.error_code is None
    assert set(record.payload) == QUALIFICATION_PAYLOAD_FIELDS
    assert record.payload == {
        "command": "verify",
        "provider": "siliconflow",
        "profile_id": profile.profile_id,
        "release_status": "review",
        "dimension": 2,
        "canary_count": 2,
        "observation_count": 3,
        "signature_fingerprint": profile.signature.fingerprint(),
        "duration_ms": 125.0,
    }
    serialized = _serialized(record)
    assert SENTINEL not in serialized
    assert "vectors" not in serialized
    assert "output_dir" not in serialized
    assert "baseline_artifact" not in serialized


def test_verify_probe_false_records_one_failed_terminal(monkeypatch, capsys) -> None:
    profile = _review_profile(with_baseline=True)
    provider = _MismatchedProvider(profile)
    logger = _CollectingLogger()
    monkeypatch.setattr(qualify, "load_profile_for_qualification", lambda profile_id: profile)
    monkeypatch.setattr(qualify, "load_canary_set", lambda selected: _canaries())
    monkeypatch.setattr(
        qualify,
        "load_baseline",
        lambda selected, canary_count: _vectors(),
    )
    monkeypatch.setattr(
        qualify,
        "create_qualification_provider",
        lambda selected, runtime, *, app_logger=None: provider,
    )

    exit_code = qualify.main(
        ["verify", "--profile", profile.profile_id],
        app_logger=logger,
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert payload["passed"] is False
    assert payload["error_code"] == "EMBEDDING_QUALIFICATION_FAILED"
    records = _qualification_records(logger)
    assert len(records) == 1
    assert records[0].event_name == "embedding.qualification_failed"
    assert records[0].level == "error"
    assert records[0].error_code == "EMBEDDING_QUALIFICATION_FAILED"
    assert records[0].payload["observation_count"] == 3


def test_early_failure_records_one_redacted_terminal_before_provider(
    monkeypatch,
    capsys,
) -> None:
    profile = _review_profile(with_baseline=False)
    logger = _CollectingLogger()
    provider_created = False

    def fail_baseline(selected, canary_count):
        raise EmbeddingQualificationError(f"profile_baseline_missing_{SENTINEL}")

    def create_provider(selected, runtime, *, app_logger=None):
        nonlocal provider_created
        provider_created = True
        raise AssertionError("must not create provider")

    monkeypatch.setattr(qualify, "load_profile_for_qualification", lambda profile_id: profile)
    monkeypatch.setattr(qualify, "load_canary_set", lambda selected: _canaries())
    monkeypatch.setattr(qualify, "load_baseline", fail_baseline)
    monkeypatch.setattr(qualify, "create_qualification_provider", create_provider)

    exit_code = qualify.main(
        ["verify", "--profile", profile.profile_id],
        app_logger=logger,
    )

    stdout = capsys.readouterr().out
    assert exit_code == 2
    assert provider_created is False
    assert SENTINEL not in stdout
    records = _qualification_records(logger)
    assert len(records) == 1
    record = records[0]
    assert record.event_name == "embedding.qualification_failed"
    assert record.error_code == "EMBEDDING_QUALIFICATION_FAILED"
    assert set(record.payload) == QUALIFICATION_PAYLOAD_FIELDS
    assert record.payload["observation_count"] == 0
    assert record.payload["signature_fingerprint"] is None
    assert SENTINEL not in _serialized(record)


def test_capture_nested_probe_records_only_one_qualification_terminal(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    profile = _review_profile(with_baseline=False)
    provider = _FakeProvider(profile)
    logger = _CollectingLogger()
    factory_loggers: list[object] = []

    def create_provider(selected, runtime, *, app_logger=None):
        factory_loggers.append(app_logger)
        return provider

    monkeypatch.setattr(qualify, "load_profile_for_qualification", lambda profile_id: profile)
    monkeypatch.setattr(qualify, "load_canary_set", lambda selected: _canaries())
    monkeypatch.setattr(qualify, "create_qualification_provider", create_provider)

    exit_code = qualify.main(
        [
            "capture-candidate",
            "--profile",
            profile.profile_id,
            "--output-dir",
            str(tmp_path / "candidate"),
        ],
        app_logger=logger,
    )

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)["passed"] is True
    assert factory_loggers == [logger]
    records = _qualification_records(logger)
    assert len(records) == 1
    assert records[0].event_name == "embedding.qualification_completed"
    assert records[0].payload["command"] == "capture-candidate"
    assert records[0].payload["observation_count"] == 4


def test_qualification_logger_failure_does_not_change_success(
    monkeypatch,
    capsys,
) -> None:
    class _FailingLogger:
        def record(self, record: AppLogRecord) -> None:
            raise RuntimeError(SENTINEL)

    profile = _review_profile(with_baseline=True)
    provider = _FakeProvider(profile)
    logger = _FailingLogger()
    factory_loggers: list[object] = []

    def create_provider(selected, runtime, *, app_logger=None):
        factory_loggers.append(app_logger)
        return provider

    monkeypatch.setattr(qualify, "load_profile_for_qualification", lambda profile_id: profile)
    monkeypatch.setattr(qualify, "load_canary_set", lambda selected: _canaries())
    monkeypatch.setattr(
        qualify,
        "load_baseline",
        lambda selected, canary_count: _vectors(),
    )
    monkeypatch.setattr(qualify, "create_qualification_provider", create_provider)

    exit_code = qualify.main(
        ["verify", "--profile", profile.profile_id],
        app_logger=logger,
    )

    stdout = capsys.readouterr().out
    assert exit_code == 0
    assert json.loads(stdout)["passed"] is True
    assert SENTINEL not in stdout
    assert factory_loggers == [logger]


def test_batch_size_one_is_provider_chunk_size_not_logical_probe_limit(
    monkeypatch,
    capsys,
) -> None:
    profile = _review_profile(with_baseline=True)
    provider = _FakeProvider(profile)
    observed_runtime_batch_sizes: list[int] = []

    def create_provider(selected, runtime, *, app_logger=None):
        observed_runtime_batch_sizes.append(runtime.batch_size)
        return provider

    monkeypatch.setattr(qualify, "load_profile_for_qualification", lambda profile_id: profile)
    monkeypatch.setattr(qualify, "load_canary_set", lambda selected: _canaries())
    monkeypatch.setattr(
        qualify,
        "load_baseline",
        lambda selected, canary_count: _vectors(),
    )
    monkeypatch.setattr(qualify, "create_qualification_provider", create_provider)

    exit_code = qualify.main(
        ["verify", "--profile", profile.profile_id, "--batch-size", "1"]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert observed_runtime_batch_sizes == [1]
    assert payload["logical_batch_size"] == 2
    assert "batch_size" not in payload
    assert [len(request.texts) for request in provider.requests] == [2, 2, 1, 1]
