from __future__ import annotations

from threading import Lock, Thread

import pytest

from app.runtime.guardrails.errors import RuntimeBusyError
from app.runtime.streaming import ActiveStreamRegistry


class _Handle:
    def __init__(self, name: str) -> None:
        self.handle_id = name
        self.turn_id = f"turn-{name}"
        self.owns_in_progress_turn = True
        self._lock = Lock()
        self.cancel_count = 0
        self.close_count = 0
        self.reason = None

    def cancel(self, *, reason: str) -> None:
        with self._lock:
            self.cancel_count += 1
            self.reason = reason

    def close(self) -> None:
        with self._lock:
            self.close_count += 1


def test_active_registry_close_cancels_closes_and_rejects_new() -> None:
    registry = ActiveStreamRegistry()
    handles = [_Handle("one"), _Handle("two")]
    for handle in handles:
        registry.register(handle)
    assert registry.is_live_turn(turn_id="turn-one")

    registry.close()
    registry.close()

    assert registry.active_count == 0
    assert not registry.is_open
    assert [(handle.cancel_count, handle.close_count) for handle in handles] == [
        (1, 1),
        (1, 1),
    ]
    assert all(handle.reason == "runtime_closing" for handle in handles)
    with pytest.raises(RuntimeBusyError, match="stream_registry_closing"):
        registry.register(_Handle("late"))


def test_active_registry_concurrent_close_runs_each_handle_once() -> None:
    registry = ActiveStreamRegistry()
    handle = _Handle("only")
    registry.register(handle)
    threads = [Thread(target=registry.close) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=1)

    assert all(not thread.is_alive() for thread in threads)
    assert handle.cancel_count == 1
    assert handle.close_count == 1
    assert registry.active_count == 0
