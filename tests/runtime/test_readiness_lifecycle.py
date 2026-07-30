from __future__ import annotations

import threading
from pathlib import Path
from time import monotonic, sleep
from types import SimpleNamespace

from app.runtime.readiness import (
    ApplicationReadiness,
    CachedFilesystemReadinessProbe,
    LifecycleReadinessState,
    ReadinessReason,
)


class _DatabaseProbe:
    def __init__(self, result: bool = True):
        self.result = result
        self.calls = 0

    def check_readable(self, deadline: float) -> bool:
        self.calls += 1
        return self.result and monotonic() < deadline


class _FilesystemProbe:
    def __init__(self, result: bool = True):
        self.result = result
        self.calls = 0

    def check(self, deadline: float) -> bool:
        self.calls += 1
        return self.result and monotonic() < deadline


def test_readiness_lifecycle_is_explicit_and_short_circuits_probes():
    lifecycle = LifecycleReadinessState()
    database = _DatabaseProbe()
    filesystem = _FilesystemProbe()
    readiness = ApplicationReadiness(
        lifecycle=lifecycle,
        database=database,
        filesystem=filesystem,
    )

    starting = readiness.check(monotonic() + 1)
    lifecycle.mark_ready()
    ready = readiness.check(monotonic() + 1)
    lifecycle.mark_closing()
    closing = readiness.check(monotonic() + 1)

    assert starting.reason == ReadinessReason.STARTING
    assert ready.ready is True
    assert closing.reason == ReadinessReason.CLOSING
    assert database.calls == 1
    assert filesystem.calls == 1


def test_readiness_uses_stable_failure_reasons():
    lifecycle = LifecycleReadinessState()
    lifecycle.mark_ready()
    database_failure = ApplicationReadiness(
        lifecycle=lifecycle,
        database=_DatabaseProbe(False),
        filesystem=_FilesystemProbe(),
    )
    filesystem_failure = ApplicationReadiness(
        lifecycle=lifecycle,
        database=_DatabaseProbe(),
        filesystem=_FilesystemProbe(False),
    )

    assert database_failure.check(monotonic() + 1).reason == ReadinessReason.STORAGE_UNAVAILABLE
    assert (
        filesystem_failure.check(monotonic() + 1).reason
        == ReadinessReason.FILESYSTEM_UNAVAILABLE
    )
    assert database_failure.check(monotonic() - 1).reason == ReadinessReason.CHECK_TIMEOUT


def test_readiness_rechecks_total_deadline_after_each_adapter():
    lifecycle = LifecycleReadinessState()
    lifecycle.mark_ready()

    class _SlowDatabase:
        def check_readable(self, deadline: float) -> bool:
            del deadline
            sleep(0.01)
            return True

    readiness = ApplicationReadiness(
        lifecycle=lifecycle,
        database=_SlowDatabase(),
        filesystem=_FilesystemProbe(),
    )

    assert readiness.check(monotonic() + 0.001).reason == ReadinessReason.CHECK_TIMEOUT


def test_filesystem_probe_caches_success(monkeypatch, tmp_path: Path):
    calls = 0

    def _disk_usage(_path: Path):
        nonlocal calls
        calls += 1
        return SimpleNamespace(free=10_000)

    monkeypatch.setattr("app.runtime.readiness.shutil.disk_usage", _disk_usage)
    probe = CachedFilesystemReadinessProbe(
        (tmp_path,),
        min_free_bytes=1,
        cache_ttl_seconds=5,
    )

    assert probe.check(monotonic() + 1) is True
    assert probe.check(monotonic() + 1) is True
    assert calls == 1


def test_filesystem_probe_is_single_flight(monkeypatch, tmp_path: Path):
    calls = 0
    entered = threading.Event()
    release = threading.Event()
    results: list[bool] = []

    def _disk_usage(_path: Path):
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(timeout=2)
        return SimpleNamespace(free=10_000)

    monkeypatch.setattr("app.runtime.readiness.shutil.disk_usage", _disk_usage)
    probe = CachedFilesystemReadinessProbe(
        (tmp_path,),
        min_free_bytes=1,
        cache_ttl_seconds=5,
    )

    first = threading.Thread(target=lambda: results.append(probe.check(monotonic() + 2)))
    second = threading.Thread(target=lambda: results.append(probe.check(monotonic() + 2)))
    first.start()
    assert entered.wait(timeout=1)
    second.start()
    release.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert results == [True, True]
    assert calls == 1
