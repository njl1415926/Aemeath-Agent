from __future__ import annotations

import shutil
import threading
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from time import monotonic
from typing import Protocol


class ReadinessReason(str, Enum):
    READY = "ready"
    STARTING = "starting"
    CLOSING = "closing"
    STORAGE_UNAVAILABLE = "storage_unavailable"
    FILESYSTEM_UNAVAILABLE = "filesystem_unavailable"
    CHECK_TIMEOUT = "check_timeout"


@dataclass(frozen=True)
class ReadinessResult:
    ready: bool
    reason: ReadinessReason


class ReadinessPort(Protocol):
    def check(self, deadline: float) -> ReadinessResult: ...


class DatabaseReadinessPort(Protocol):
    def check_readable(self, deadline: float) -> bool: ...


class FilesystemReadinessPort(Protocol):
    def check(self, deadline: float) -> bool: ...


class LifecycleReadinessState:
    def __init__(self) -> None:
        self._state = ReadinessReason.STARTING
        self._lock = threading.Lock()

    def mark_ready(self) -> None:
        with self._lock:
            if self._state == ReadinessReason.STARTING:
                self._state = ReadinessReason.READY

    def mark_closing(self) -> None:
        with self._lock:
            self._state = ReadinessReason.CLOSING

    def snapshot(self) -> ReadinessReason:
        with self._lock:
            return self._state


class CachedFilesystemReadinessProbe:
    def __init__(
        self,
        paths: tuple[Path, ...],
        *,
        min_free_bytes: int,
        cache_ttl_seconds: float,
    ) -> None:
        self._paths = tuple(dict.fromkeys(path.resolve() for path in paths))
        self._min_free_bytes = min_free_bytes
        self._cache_ttl_seconds = cache_ttl_seconds
        self._condition = threading.Condition()
        self._checking = False
        self._cached_result: bool | None = None
        self._cached_at = 0.0

    def check(self, deadline: float) -> bool:
        with self._condition:
            while True:
                now = monotonic()
                if (
                    self._cached_result is not None
                    and now - self._cached_at < self._cache_ttl_seconds
                ):
                    return self._cached_result
                if not self._checking:
                    self._checking = True
                    break
                remaining = deadline - now
                if remaining <= 0:
                    return False
                self._condition.wait(timeout=remaining)

        try:
            result = self._inspect_paths(deadline)
        except Exception:
            result = False
        with self._condition:
            self._cached_result = result
            self._cached_at = monotonic()
            self._checking = False
            self._condition.notify_all()
        return result

    def _inspect_paths(self, deadline: float) -> bool:
        try:
            for path in self._paths:
                if monotonic() >= deadline or not path.is_dir():
                    return False
                free_bytes = shutil.disk_usage(path).free
                if monotonic() >= deadline or free_bytes < self._min_free_bytes:
                    return False
            return True
        except OSError:
            return False


class ApplicationReadiness(ReadinessPort):
    def __init__(
        self,
        *,
        lifecycle: LifecycleReadinessState,
        database: DatabaseReadinessPort,
        filesystem: FilesystemReadinessPort,
    ) -> None:
        self._lifecycle = lifecycle
        self._database = database
        self._filesystem = filesystem

    def check(self, deadline: float) -> ReadinessResult:
        state = self._lifecycle.snapshot()
        if state != ReadinessReason.READY:
            return ReadinessResult(ready=False, reason=state)
        if monotonic() >= deadline:
            return ReadinessResult(ready=False, reason=ReadinessReason.CHECK_TIMEOUT)
        if not self._database.check_readable(deadline):
            reason = (
                ReadinessReason.CHECK_TIMEOUT
                if monotonic() >= deadline
                else ReadinessReason.STORAGE_UNAVAILABLE
            )
            return ReadinessResult(ready=False, reason=reason)
        if not self._filesystem.check(deadline):
            reason = (
                ReadinessReason.CHECK_TIMEOUT
                if monotonic() >= deadline
                else ReadinessReason.FILESYSTEM_UNAVAILABLE
            )
            return ReadinessResult(ready=False, reason=reason)
        if monotonic() >= deadline:
            return ReadinessResult(ready=False, reason=ReadinessReason.CHECK_TIMEOUT)
        return ReadinessResult(ready=True, reason=ReadinessReason.READY)
