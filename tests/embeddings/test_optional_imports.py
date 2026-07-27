from __future__ import annotations

import builtins
import subprocess
import sys
from pathlib import Path
from typing import Callable

import pytest

from app.embeddings.config import load_profile_for_qualification
from app.embeddings.errors import EmbeddingDependencyError
from app.embeddings.providers.local_bge_m3 import BgeM3EmbeddingProvider
from app.embeddings.providers.siliconflow import SiliconFlowEmbeddingProvider
from app.embeddings.schemas import EmbeddingRequest, EmbeddingRuntimeOptions


def test_foundation_and_legacy_shim_import_without_optional_dependencies() -> None:
    project_root = Path(__file__).resolve().parents[2]
    script = r'''
import builtins
import sys

blocked = {"httpx", "torch", "transformers"}
real_import = builtins.__import__

def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    if name.partition(".")[0] in blocked:
        raise AssertionError(f"optional dependency imported eagerly: {name}")
    return real_import(name, globals, locals, fromlist, level)

builtins.__import__ = guarded_import
import app.embeddings
import app.retrieval.embeddings

assert not blocked.intersection(sys.modules)
print("optional-imports-not-loaded")
'''

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=project_root,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "optional-imports-not-loaded"


def _block_imports(
    monkeypatch: pytest.MonkeyPatch,
    blocked: set[str],
) -> None:
    real_import: Callable[..., object] = builtins.__import__

    def guarded_import(
        name: str,
        globals=None,
        locals=None,
        fromlist=(),
        level: int = 0,
    ):
        if name.partition(".")[0] in blocked:
            raise ModuleNotFoundError(f"synthetically blocked optional dependency: {name}")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded_import)


def test_remote_provider_reports_typed_error_when_http_extra_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = load_profile_for_qualification("siliconflow_pro_bge_m3_v1")
    provider = SiliconFlowEmbeddingProvider(
        profile=profile,
        runtime=EmbeddingRuntimeOptions(max_retries=0),
        api_key="synthetic-unit-test-key",
    )
    _block_imports(monkeypatch, {"httpx"})

    with pytest.raises(EmbeddingDependencyError) as exc_info:
        provider.embed(EmbeddingRequest(texts=("offline dependency probe",), purpose="query"))

    assert exc_info.value.reason == "embedding_api_extra_missing"


def test_local_provider_reports_typed_error_when_local_extra_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = load_profile_for_qualification("local_bge_m3_v1")
    provider = BgeM3EmbeddingProvider(
        model_path=tmp_path / "model",
        profile=profile,
    )
    _block_imports(monkeypatch, {"torch", "transformers"})

    with pytest.raises(EmbeddingDependencyError) as exc_info:
        provider.embed(
            EmbeddingRequest(texts=("offline dependency probe",), purpose="document")
        )

    assert exc_info.value.reason == "embedding_local_extra_missing"
