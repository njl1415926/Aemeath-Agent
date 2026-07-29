from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from threading import Lock
from time import monotonic
from typing import Iterator

from app.runtime.guardrails.concurrency import ConcurrencyGate
from app.runtime.guardrails.errors import RuntimeBusyError
from app.runtime.guardrails.schemas import DialogueAdmissionLease, DialogueAdmissionRequest


@dataclass
class _SessionEntry:
    lock: Lock
    reference_count: int
    last_used_monotonic: float


class InProcessDialogueAdmission:
    def __init__(
        self,
        *,
        session_wait_timeout_seconds: float,
        dialogue_limit: int,
        dialogue_wait_timeout_seconds: float,
    ):
        if session_wait_timeout_seconds < 0:
            raise ValueError("invalid session wait timeout")
        self._session_wait_timeout_seconds = session_wait_timeout_seconds
        self._dialogue_gate = ConcurrencyGate(
            limit=dialogue_limit,
            wait_timeout_seconds=dialogue_wait_timeout_seconds,
            reason="dialogue_concurrency",
        )
        self._registry_lock = Lock()
        self._entries: dict[tuple[str, str], _SessionEntry] = {}

    @contextmanager
    def acquire(
        self, *, request: DialogueAdmissionRequest
    ) -> Iterator[DialogueAdmissionLease]:
        if not request.user_id or not request.session_id:
            raise ValueError("admission user_id and session_id are required")
        key = (request.user_id, request.session_id)
        entry = self._reference_entry(key)
        session_started = monotonic()
        session_acquired = entry.lock.acquire(timeout=self._session_wait_timeout_seconds)
        session_wait_ms = max(0.0, (monotonic() - session_started) * 1000.0)
        if not session_acquired:
            self._dereference_entry(key, entry)
            raise RuntimeBusyError("session_lock_wait_timeout")
        try:
            with self._dialogue_gate.acquire() as dialogue_wait_ms:
                yield DialogueAdmissionLease(
                    session_wait_ms=session_wait_ms,
                    dialogue_wait_ms=dialogue_wait_ms,
                )
        finally:
            entry.lock.release()
            self._dereference_entry(key, entry)

    def _reference_entry(self, key: tuple[str, str]) -> _SessionEntry:
        now = monotonic()
        with self._registry_lock:
            entry = self._entries.get(key)
            if entry is None:
                entry = _SessionEntry(Lock(), 0, now)
                self._entries[key] = entry
            entry.reference_count += 1
            entry.last_used_monotonic = now
            return entry

    def _dereference_entry(self, key: tuple[str, str], entry: _SessionEntry) -> None:
        with self._registry_lock:
            entry.reference_count -= 1
            entry.last_used_monotonic = monotonic()
            if entry.reference_count == 0 and self._entries.get(key) is entry:
                del self._entries[key]

    @property
    def entry_count(self) -> int:
        with self._registry_lock:
            return len(self._entries)
