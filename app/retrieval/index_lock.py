from __future__ import annotations

import math
import os
import time
from pathlib import Path
from types import TracebackType

from app.retrieval.errors import RagIndexLockError


class CrossProcessFileLock:
    """Small cross-platform exclusive OS lock with bounded acquisition."""

    def __init__(
        self,
        path: Path,
        *,
        timeout_seconds: float = 5.0,
        poll_seconds: float = 0.05,
    ) -> None:
        values = (timeout_seconds, poll_seconds)
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            for value in values
        ) or timeout_seconds < 0 or poll_seconds <= 0:
            raise ValueError("rag_index_lock_timeout_invalid")
        self.path = path
        self.timeout_seconds = timeout_seconds
        self.poll_seconds = poll_seconds
        self._file = None

    def acquire(self) -> None:
        if self._file is not None:
            raise RagIndexLockError("rag_index_lock_already_held")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = self.path.open("a+b")
        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b"\0")
            lock_file.flush()
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            try:
                self._lock_file(lock_file)
                self._file = lock_file
                return
            except OSError as exc:
                if time.monotonic() >= deadline:
                    lock_file.close()
                    raise RagIndexLockError("rag_index_build_in_progress") from exc
                time.sleep(min(self.poll_seconds, max(0.0, deadline - time.monotonic())))

    def release(self) -> None:
        lock_file = self._file
        if lock_file is None:
            return
        self._file = None
        try:
            self._unlock_file(lock_file)
        finally:
            lock_file.close()

    def _lock_file(self, lock_file) -> None:
        lock_file.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            return
        import fcntl

        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock_file(self, lock_file) -> None:
        lock_file.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            return
        import fcntl

        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def __enter__(self) -> CrossProcessFileLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()
