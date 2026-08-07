from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from threading import Condition, Lock, get_ident
from time import monotonic
from typing import Iterator

from app.runtime.guardrails.concurrency import ConcurrencyGate, ConcurrencyPermitHandle
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
        handle = self.open_handle(request=request)
        try:
            yield handle.lease
        finally:
            handle.close()

    def open_handle(
        self,
        *,
        request: DialogueAdmissionRequest,
    ) -> InProcessDialogueAdmissionHandle:
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
            permit = self._dialogue_gate.open_handle()
        except BaseException:
            entry.lock.release()
            self._dereference_entry(key, entry)
            raise
        return InProcessDialogueAdmissionHandle(
            owner=self,
            key=key,
            entry=entry,
            permit=permit,
            lease=DialogueAdmissionLease(
                session_wait_ms=session_wait_ms,
                dialogue_wait_ms=permit.wait_ms,
            ),
        )

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


class InProcessDialogueAdmissionHandle:
    """Owns one Session lock and dialogue permit across thread boundaries."""

    def __init__(
        self,
        *,
        owner: InProcessDialogueAdmission,
        key: tuple[str, str],
        entry: _SessionEntry,
        permit: ConcurrencyPermitHandle,
        lease: DialogueAdmissionLease,
    ) -> None:
        self._owner = owner
        self._key = key
        self._entry = entry
        self._permit = permit
        self._lease = lease
        self._condition = Condition(Lock())
        self._state = "open"
        self._closing_thread_id: int | None = None

    @property
    def lease(self) -> DialogueAdmissionLease:
        return self._lease

    def close(self) -> None:
        current_thread_id = get_ident()
        with self._condition:
            if self._state == "closed":
                return
            if self._state == "closing":
                if self._closing_thread_id == current_thread_id:
                    return
                deadline = monotonic() + 5.0
                while self._state != "closed":
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        return
                    self._condition.wait(timeout=remaining)
                return
            self._state = "closing"
            self._closing_thread_id = current_thread_id

        first_error: BaseException | None = None
        try:
            self._permit.close()
        except BaseException as exc:
            first_error = exc
        try:
            self._entry.lock.release()
        except BaseException as exc:
            first_error = first_error or exc
        try:
            self._owner._dereference_entry(self._key, self._entry)
        except BaseException as exc:
            first_error = first_error or exc
        finally:
            with self._condition:
                self._state = "closed"
                self._closing_thread_id = None
                self._condition.notify_all()
        if first_error is not None:
            raise first_error
