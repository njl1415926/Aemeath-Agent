from __future__ import annotations

from threading import Event, Thread

import pytest

from app.runtime.guardrails.concurrency import ConcurrencyGate
from app.runtime.guardrails.errors import RuntimeBusyError


def test_gate_enforces_limit_then_recovers_after_release():
    gate = ConcurrencyGate(limit=1, wait_timeout_seconds=0.01, reason="llm_concurrency")
    with gate.acquire():
        with pytest.raises(RuntimeBusyError) as raised:
            with gate.acquire():
                pass
        assert raised.value.reason == "llm_concurrency"
    with gate.acquire():
        assert gate.counts()[0] == 1
    assert gate.counts() == (0, 1)


def test_gate_releases_permit_when_delegate_scope_raises():
    gate = ConcurrencyGate(limit=1, wait_timeout_seconds=0.01, reason="embedding_concurrency")
    with pytest.raises(ValueError):
        with gate.acquire():
            raise ValueError("boom")
    with gate.acquire():
        pass


def test_gate_peak_count_tracks_real_parallel_holders():
    gate = ConcurrencyGate(limit=2, wait_timeout_seconds=0.1, reason="dialogue_concurrency")
    entered = [Event(), Event()]
    release = Event()

    def hold(index: int):
        with gate.acquire():
            entered[index].set()
            release.wait(timeout=2)

    threads = [Thread(target=hold, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    assert all(event.wait(timeout=1) for event in entered)
    assert gate.counts() == (2, 2)
    release.set()
    for thread in threads:
        thread.join(timeout=1)
