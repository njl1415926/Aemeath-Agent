from __future__ import annotations

from contextlib import contextmanager
from threading import Event, Thread

import pytest

from app.model.streaming import ModelStreamFinished, ModelStreamRequest
from app.runtime.guardrails.concurrency import (
    ConcurrencyGate,
    LimitedChatModelClient,
    LimitedStreamingChatModelClient,
)
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


def test_gate_explicit_permit_is_cross_thread_and_idempotent() -> None:
    gate = ConcurrencyGate(limit=1, wait_timeout_seconds=0.01, reason="dialogue")
    permit = gate.open_handle()
    threads = [Thread(target=permit.close) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=1)
    permit.close()

    assert gate.counts() == (0, 1)
    with gate.acquire():
        assert gate.counts()[0] == 1


def test_sync_and_streaming_clients_share_one_llm_gate() -> None:
    gate = ConcurrencyGate(limit=1, wait_timeout_seconds=0.01, reason="llm_concurrency")
    entered = Event()
    release = Event()

    class BlockingStream:
        def __iter__(self):
            entered.set()
            release.wait(timeout=2)
            yield ModelStreamFinished("complete")

        def cancel(self, *, reason):
            del reason
            release.set()

        def close(self):
            release.set()

    class StreamClient:
        @contextmanager
        def open_stream(self, *, request):
            del request
            stream = BlockingStream()
            try:
                yield stream
            finally:
                stream.close()

    class SyncClient:
        def chat(self, *, messages):
            del messages
            return "sync"

    streaming = LimitedStreamingChatModelClient(gate=gate, delegate=StreamClient())
    synchronous = LimitedChatModelClient(gate=gate, delegate=SyncClient())

    def consume() -> None:
        with streaming.open_stream(request=ModelStreamRequest(messages=[])) as stream:
            list(stream)

    thread = Thread(target=consume)
    thread.start()
    assert entered.wait(timeout=1)
    with pytest.raises(RuntimeBusyError) as raised:
        synchronous.chat(messages=[])
    assert raised.value.reason == "llm_concurrency"
    release.set()
    thread.join(timeout=1)
    assert gate.counts()[0] == 0
