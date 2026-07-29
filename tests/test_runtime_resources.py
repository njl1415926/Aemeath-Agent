from __future__ import annotations

from threading import Event, Thread

from app.runtime.resources import RuntimeResources


def test_resources_close_in_reverse_order_and_aggregate_safe_failures():
    resources = RuntimeResources()
    calls = []
    resources.register(name="first", close=lambda: calls.append("first"))

    def fail():
        calls.append("second")
        raise RuntimeError("secret must not be retained")

    resources.register(name="second", close=fail)
    failures = resources.close()
    assert calls == ["second", "first"]
    assert [(item.resource_name, item.error_type) for item in failures] == [
        ("second", "RuntimeError")
    ]
    assert "secret" not in repr(failures)


def test_resources_concurrent_close_calls_delegate_at_most_once():
    resources = RuntimeResources()
    calls = []
    resources.register(name="only", close=lambda: calls.append("closed"))
    threads = [Thread(target=resources.close) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=1)
    assert calls == ["closed"]
    assert resources.close() == ()


def test_resources_same_thread_reentrant_close_returns_and_all_waiters_exit():
    resources = RuntimeResources()
    entered = Event()
    release = Event()
    calls = []

    def close_reentrantly():
        calls.append("closed")
        entered.set()
        assert resources.close() == ()
        release.wait(timeout=1)

    resources.register(name="reentrant", close=close_reentrantly)
    owner = Thread(target=resources.close, daemon=True)
    owner.start()
    assert entered.wait(timeout=1)
    waiters = [Thread(target=resources.close, daemon=True) for _ in range(7)]
    for waiter in waiters:
        waiter.start()
    release.set()
    for thread in [owner, *waiters]:
        thread.join(timeout=1)

    assert all(not thread.is_alive() for thread in [owner, *waiters])
    assert calls == ["closed"]
