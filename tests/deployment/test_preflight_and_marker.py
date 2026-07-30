from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.deployment.marker import marker_is_absent
from app.deployment.preflight import (
    _require_database_target,
    _require_directory,
    _require_linux_secret_contract,
    _require_read_only_directory,
    _require_secret_file,
)


def test_writable_directory_preflight_removes_sentinel(tmp_path: Path):
    target = tmp_path / "state"
    target.mkdir(mode=0o750)

    _require_directory(target, writable=True, name="test_root")

    assert list(target.iterdir()) == []


def test_application_root_preflight_rejects_writable_directory(tmp_path: Path):
    with pytest.raises(ValueError, match="mounted read-only"):
        _require_read_only_directory(tmp_path)

    assert list(tmp_path.iterdir()) == []


def test_database_target_rejects_non_file(tmp_path: Path):
    with pytest.raises(ValueError, match="regular non-symlink"):
        _require_database_target(tmp_path)


def test_secret_file_requires_exact_approved_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    configured = tmp_path / "configured-secret"
    configured.write_text("fake-secret", encoding="utf-8")
    monkeypatch.setenv("EXAMPLE_SECRET_FILE", str(configured))

    with pytest.raises(ValueError, match="approved production path"):
        _require_secret_file("EXAMPLE_SECRET_FILE", approved_path=tmp_path / "approved-secret")


def test_linux_secret_contract_requires_runtime_and_root_group_mode(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(os, "geteuid", lambda: 10001, raising=False)
    monkeypatch.setattr(os, "getegid", lambda: 10001, raising=False)
    monkeypatch.setattr(os, "getgroups", lambda: [10001], raising=False)
    info = SimpleNamespace(st_uid=0, st_gid=10001, st_mode=stat.S_IFREG | 0o440)

    _require_linux_secret_contract(info, env_name="EXAMPLE_SECRET_FILE")


@pytest.mark.parametrize(
    ("uid", "gid", "mode"),
    [(10001, 10001, 0o440), (0, 10002, 0o440), (0, 10001, 0o640), (0, 10001, 0o460)],
)
def test_linux_secret_contract_rejects_owner_group_or_mode_drift(
    monkeypatch: pytest.MonkeyPatch,
    uid: int,
    gid: int,
    mode: int,
):
    monkeypatch.setattr(os, "geteuid", lambda: 10001, raising=False)
    monkeypatch.setattr(os, "getegid", lambda: 10001, raising=False)
    monkeypatch.setattr(os, "getgroups", lambda: [10001], raising=False)
    info = SimpleNamespace(st_uid=uid, st_gid=gid, st_mode=stat.S_IFREG | mode)

    with pytest.raises(ValueError):
        _require_linux_secret_contract(info, env_name="EXAMPLE_SECRET_FILE")


def test_marker_scanner_finds_marker_across_chunk_boundary(tmp_path: Path):
    marker_file = tmp_path / "marker"
    marker_file.write_bytes(b"unique-fake-secret-marker")
    artifact = tmp_path / "artifact.bin"
    artifact.write_bytes(b"x" * (64 * 1024 - 4) + b"unique-fake-secret-marker")

    assert marker_is_absent(marker_file=marker_file, targets=[artifact]) is False


def test_marker_cli_never_echoes_marker(tmp_path: Path):
    marker = "unique-cli-secret-marker"
    marker_file = tmp_path / "marker"
    artifact = tmp_path / "artifact"
    marker_file.write_text(marker, encoding="utf-8")
    artifact.write_text(f"prefix-{marker}-suffix", encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            "scripts/deployment/check_secret_marker.py",
            "--marker-file",
            str(marker_file),
            "--category",
            "test-artifact",
            str(artifact),
        ],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        check=False,
        text=True,
        encoding="utf-8",
    )

    assert completed.returncode == 1
    assert json.loads(completed.stdout) == {"category": "test-artifact", "status": "fail"}
    assert marker not in completed.stdout
    assert marker not in completed.stderr


def test_preflight_cli_handles_non_value_configuration_error_without_traceback():
    completed = subprocess.run(
        [sys.executable, "scripts/deployment/preflight.py"],
        cwd=Path(__file__).resolve().parents[2],
        env={
            **os.environ,
            "AEMEATH_ENVIRONMENT": "test",
            "AEMEATH_EMBEDDING_BATCH_SIZE": "unique-invalid-integer-marker",
        },
        capture_output=True,
        check=False,
        text=True,
        encoding="utf-8",
    )

    assert completed.returncode == 1
    assert json.loads(completed.stdout) == {
        "reason": "configuration_invalid",
        "status": "failed",
    }
    assert "unique-invalid-integer-marker" not in completed.stdout
    assert completed.stderr == ""
