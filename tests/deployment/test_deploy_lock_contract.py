from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from app.deployment.lock_contract import validate_deployment_lock

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_deployment_lock_covers_pyproject_runtime_api_and_uvicorn_standard():
    pyproject_text = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    lock_text = (PROJECT_ROOT / "requirements.deploy.lock").read_text(encoding="utf-8")

    report = validate_deployment_lock(pyproject_text=pyproject_text, lock_text=lock_text)

    assert report.valid


def test_deployment_lock_check_detects_missing_uvicorn_standard_package():
    pyproject_text = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    lock_text = (PROJECT_ROOT / "requirements.deploy.lock").read_text(encoding="utf-8")
    lock_text = lock_text.replace(
        "uvloop==0.22.1 \\\n"
        "    --hash=sha256:481c990a7abe2c6f4fc3d98781cc9426ebd7f03a9aaa7eb03d3bfc68ac2a46bd\n",
        "",
    )

    report = validate_deployment_lock(pyproject_text=pyproject_text, lock_text=lock_text)

    assert report.missing_packages == ("uvloop",)


def test_deployment_lock_check_detects_missing_python310_conditional_package():
    pyproject_text = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    lock_text = (PROJECT_ROOT / "requirements.deploy.lock").read_text(encoding="utf-8")
    lock_text = lock_text.replace(
        'exceptiongroup==1.3.1 ; python_version < "3.11" \\\n'
        "    --hash=sha256:a7a39a3bd276781e98394987d3a5701d0c4edffb633bb7a5144577f82c773598\n",
        "",
    )

    report = validate_deployment_lock(pyproject_text=pyproject_text, lock_text=lock_text)

    assert report.missing_packages == ("exceptiongroup",)


@pytest.mark.parametrize(
    ("declared", "incompatible", "package_name"),
    [
        ('  "numpy>=1.26.0",', '  "numpy>=3",', "numpy"),
        ('  "fastapi>=0.115,<1",', '  "fastapi>=2,<3",', "fastapi"),
    ],
)
def test_deployment_lock_check_detects_pep440_constraint_mismatch(
    declared: str,
    incompatible: str,
    package_name: str,
):
    pyproject_text = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    pyproject_text = pyproject_text.replace(declared, incompatible)
    lock_text = (PROJECT_ROOT / "requirements.deploy.lock").read_text(encoding="utf-8")

    report = validate_deployment_lock(pyproject_text=pyproject_text, lock_text=lock_text)

    assert report.constraint_mismatches == (package_name,)
    assert report.valid is False


def test_deployment_lock_cli_reports_clean_contract():
    completed = subprocess.run(
        [sys.executable, "scripts/deployment/check_deploy_lock.py"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        check=False,
        text=True,
        encoding="utf-8",
    )

    assert completed.returncode == 0
    assert json.loads(completed.stdout) == {
        "constraint_mismatches": [],
        "missing_packages": [],
        "status": "ok",
        "unhashed_packages": [],
        "unpinned_packages": [],
    }
