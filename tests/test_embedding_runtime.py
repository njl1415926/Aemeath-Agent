from __future__ import annotations

from threading import Event, Thread

import numpy as np
import pytest

from app.embeddings.config import load_embedding_space_profile
from app.embeddings.errors import EmbeddingConfigurationError
from app.embeddings.providers.siliconflow import SiliconFlowEmbeddingProvider
from app.embeddings.runtime import EmbeddingProviderRuntime
from app.embeddings.schemas import EmbeddingRequest, EmbeddingRuntimeOptions


class _Closeable:
    def __init__(self):
        self.close_count = 0

    def close(self):
        self.close_count += 1


def test_embedding_runtime_close_is_thread_safe_and_at_most_once():
    closeable = _Closeable()
    runtime = EmbeddingProviderRuntime(provider=object(), _close=closeable.close)  # type: ignore[arg-type]
    threads = [Thread(target=runtime.close) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=1)
    assert closeable.close_count == 1


def test_embedding_runtime_same_thread_reentrant_close_releases_all_waiters():
    entered = Event()
    release = Event()
    calls = []
    runtime = None

    def close_reentrantly():
        calls.append("closed")
        entered.set()
        assert runtime is not None
        runtime.close()
        release.wait(timeout=1)

    runtime = EmbeddingProviderRuntime(
        provider=object(),  # type: ignore[arg-type]
        _close=close_reentrantly,
    )
    owner = Thread(target=runtime.close, daemon=True)
    owner.start()
    assert entered.wait(timeout=1)
    waiters = [Thread(target=runtime.close, daemon=True) for _ in range(7)]
    for waiter in waiters:
        waiter.start()
    release.set()
    for thread in [owner, *waiters]:
        thread.join(timeout=1)

    assert all(not thread.is_alive() for thread in [owner, *waiters])
    assert calls == ["closed"]


def test_embedding_runtime_close_failure_still_transitions_to_closed():
    calls = []

    def fail_close():
        calls.append(True)
        raise RuntimeError("close failed")

    runtime = EmbeddingProviderRuntime(provider=object(), _close=fail_close)  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="close failed"):
        runtime.close()
    runtime.close()

    assert calls == [True]


def _siliconflow_provider(*, client=None, client_factory=None):
    return SiliconFlowEmbeddingProvider(
        profile=load_embedding_space_profile("siliconflow_pro_bge_m3_v1"),
        runtime=EmbeddingRuntimeOptions(max_retries=0),
        api_key="test-only-key",
        client=client,
        client_factory=client_factory,
    )


def test_siliconflow_close_before_lazy_create_forbids_future_embed():
    factory_calls = []
    provider = _siliconflow_provider(
        client_factory=lambda: factory_calls.append(True) or _Closeable()
    )
    provider.close()
    with pytest.raises(EmbeddingConfigurationError) as raised:
        provider.embed(EmbeddingRequest(texts=(), purpose="query"))
    assert raised.value.reason == "siliconflow_provider_closed"
    assert factory_calls == []


def test_siliconflow_injected_client_is_never_closed():
    client = _Closeable()
    provider = _siliconflow_provider(client=client)
    provider.close()
    assert client.close_count == 0


class _BlockingResponse:
    status_code = 200
    headers = {}

    def json(self):
        return {
            "model": "Pro/BAAI/bge-m3",
            "data": [{"index": 0, "embedding": [1.0] + [0.0] * 1023}],
        }


class _BlockingClient(_Closeable):
    def __init__(self):
        super().__init__()
        self.entered = Event()
        self.release = Event()

    def post(self, *_args, **_kwargs):
        self.entered.set()
        self.release.wait(timeout=2)
        return _BlockingResponse()


def test_siliconflow_close_waits_for_active_embed_then_closes_owned_client():
    client = _BlockingClient()
    provider = _siliconflow_provider(client_factory=lambda: client)
    results = []
    embed_thread = Thread(
        target=lambda: results.append(
            provider.embed(EmbeddingRequest(texts=("query",), purpose="query"))
        )
    )
    embed_thread.start()
    assert client.entered.wait(timeout=1)
    close_thread = Thread(target=provider.close)
    close_thread.start()
    close_thread.join(timeout=0.03)
    assert close_thread.is_alive()
    client.release.set()
    embed_thread.join(timeout=1)
    close_thread.join(timeout=1)
    assert results[0].vectors.shape == (1, 1024)
    assert np.isclose(np.linalg.norm(results[0].vectors[0]), 1.0)
    assert client.close_count == 1


def test_siliconflow_close_failure_forbids_reopen_and_is_not_retried():
    class _FailingCloseClient(_BlockingClient):
        def close(self):
            self.close_count += 1
            raise RuntimeError("client close failed")

    client = _FailingCloseClient()
    client.release.set()
    provider = _siliconflow_provider(client_factory=lambda: client)
    provider.embed(EmbeddingRequest(texts=("query",), purpose="query"))

    with pytest.raises(RuntimeError, match="client close failed"):
        provider.close()
    provider.close()
    with pytest.raises(EmbeddingConfigurationError) as raised:
        provider.embed(EmbeddingRequest(texts=("query",), purpose="query"))

    assert raised.value.reason == "siliconflow_provider_closed"
    assert client.close_count == 1


def test_siliconflow_rejects_ambiguous_client_ownership():
    with pytest.raises(EmbeddingConfigurationError) as raised:
        _siliconflow_provider(client=_Closeable(), client_factory=_Closeable)
    assert raised.value.reason == "siliconflow_client_ownership_ambiguous"
