from __future__ import annotations

from contextlib import AbstractContextManager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from app.embeddings.errors import EmbeddingInvalidResponseError
from app.embeddings.providers.local_bge_m3 import BgeM3EmbeddingProvider
from app.embeddings.schemas import EmbeddingRequest


class _FakeTensor:
    def __init__(self, value: object):
        self.value = np.asarray(value)
        self.moves: list[str] = []

    def to(self, device: str) -> _FakeTensor:
        self.moves.append(device)
        return self

    def __getitem__(self, key: object) -> _FakeTensor:
        return _FakeTensor(self.value[key])

    def detach(self) -> _FakeTensor:
        return self

    def cpu(self) -> _FakeTensor:
        return self

    def numpy(self) -> np.ndarray:
        return self.value


class _NoGrad(AbstractContextManager[None]):
    def __init__(self, owner: _FakeTorch):
        self._owner = owner

    def __enter__(self) -> None:
        self._owner.no_grad_entries += 1
        return None

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        return None


class _FakeCuda:
    def __init__(self, available: bool):
        self._available = available

    def is_available(self) -> bool:
        return self._available


class _FakeTorch:
    def __init__(self, *, cuda_available: bool = False):
        self.cuda = _FakeCuda(cuda_available)
        self.no_grad_entries = 0

    def no_grad(self) -> _NoGrad:
        return _NoGrad(self)


class _FakeTokenizer:
    def __init__(self):
        self.calls: list[dict[str, Any]] = []
        self.encoded_tensors: list[_FakeTensor] = []

    def __call__(
        self,
        texts: list[str],
        *,
        padding: bool,
        truncation: bool,
        max_length: int,
        return_tensors: str,
    ) -> dict[str, _FakeTensor]:
        self.calls.append(
            {
                "texts": tuple(texts),
                "padding": padding,
                "truncation": truncation,
                "max_length": max_length,
                "return_tensors": return_tensors,
            }
        )
        ids = [int(text.rsplit("-", 1)[-1]) for text in texts]
        tensor = _FakeTensor(ids)
        self.encoded_tensors.append(tensor)
        return {"input_ids": tensor}


class _FakeModel:
    def __init__(self, *, output_kind: str = "valid"):
        self.device = "fake-device"
        self.output_kind = output_kind
        self.call_count = 0

    def __call__(self, *, input_ids: _FakeTensor):
        self.call_count += 1
        ids = input_ids.value.astype(int).tolist()
        dimension = 1023 if self.output_kind == "wrong_dimension" else 1024
        hidden = np.zeros((len(ids), 2, dimension), dtype=np.float64)
        if self.output_kind == "valid":
            for row, item_id in enumerate(ids):
                hidden[row, 0, item_id] = float(item_id + 1)
                hidden[row, 1, (item_id + 100) % dimension] = 999.0
        elif self.output_kind == "non_finite":
            hidden[:, 0, 0] = np.nan
        elif self.output_kind == "wrong_dimension":
            hidden[:, 0, 0] = 1.0
        elif self.output_kind != "zero":
            raise AssertionError(f"unknown fake output kind: {self.output_kind}")
        return SimpleNamespace(last_hidden_state=_FakeTensor(hidden))


def _install_fake_load(
    provider: BgeM3EmbeddingProvider,
    *,
    output_kind: str = "valid",
) -> tuple[_FakeTokenizer, _FakeModel, _FakeTorch]:
    tokenizer = _FakeTokenizer()
    model = _FakeModel(output_kind=output_kind)
    torch = _FakeTorch()
    provider._load = lambda: (tokenizer, model, torch)  # type: ignore[method-assign]
    return tokenizer, model, torch


def test_local_bge_empty_batch_does_not_load_model(tmp_path: Path) -> None:
    provider = BgeM3EmbeddingProvider(model_path=tmp_path / "missing-model")

    def fail_load():
        raise AssertionError("empty batches must remain lazy")

    provider._load = fail_load  # type: ignore[method-assign]
    result = provider.embed(EmbeddingRequest(texts=(), purpose="document"))

    assert result.vectors.shape == (0, 1024)
    assert result.vectors.dtype == np.dtype(np.float32)
    assert result.usage is not None and result.usage.input_count == 0


def test_local_bge_uses_cls_pooling_internal_batches_and_max_length(tmp_path: Path) -> None:
    provider = BgeM3EmbeddingProvider(
        model_path=tmp_path / "model",
        batch_size=2,
        max_length=77,
        device="auto",
    )
    tokenizer, model, torch = _install_fake_load(provider)

    result = provider.embed(
        EmbeddingRequest(
            texts=("text-0", "text-1", "text-2", "text-3", "text-4"),
            purpose="query",
        )
    )

    assert [call["texts"] for call in tokenizer.calls] == [
        ("text-0", "text-1"),
        ("text-2", "text-3"),
        ("text-4",),
    ]
    assert all(
        {
            "padding": call["padding"],
            "truncation": call["truncation"],
            "max_length": call["max_length"],
            "return_tensors": call["return_tensors"],
        }
        == {
            "padding": True,
            "truncation": True,
            "max_length": 77,
            "return_tensors": "pt",
        }
        for call in tokenizer.calls
    )
    assert all(tensor.moves == [model.device] for tensor in tokenizer.encoded_tensors)
    assert model.call_count == 3
    assert torch.no_grad_entries == 3
    assert result.vectors.shape == (5, 1024)
    assert result.vectors.dtype == np.dtype(np.float32)
    assert result.vectors.flags.c_contiguous
    np.testing.assert_allclose(result.vectors[:, :5], np.eye(5, dtype=np.float32))
    np.testing.assert_allclose(result.vectors[:, 100:105], np.zeros((5, 5)))
    np.testing.assert_allclose(np.linalg.norm(result.vectors, axis=1), np.ones(5))
    assert result.usage is not None and result.usage.input_count == 5


@pytest.mark.parametrize(
    ("output_kind", "reason"),
    [
        ("wrong_dimension", "local_model_output_shape"),
        ("non_finite", "local_model_output_non_finite"),
        ("zero", "local_model_output_zero_norm"),
    ],
)
def test_local_bge_rejects_corrupt_model_output(
    tmp_path: Path,
    output_kind: str,
    reason: str,
) -> None:
    provider = BgeM3EmbeddingProvider(model_path=tmp_path / "model")
    _install_fake_load(provider, output_kind=output_kind)

    with pytest.raises(EmbeddingInvalidResponseError) as raised:
        provider.embed(EmbeddingRequest(texts=("text-0",), purpose="document"))
    assert raised.value.reason == reason


def test_local_bge_auto_device_resolution_is_explicit_and_testable(tmp_path: Path) -> None:
    auto = BgeM3EmbeddingProvider(model_path=tmp_path / "model", device="auto")
    assert auto._resolve_device(_FakeTorch(cuda_available=True)) == "cuda"
    assert auto._resolve_device(_FakeTorch(cuda_available=False)) == "cpu"

    inherited = BgeM3EmbeddingProvider(model_path=tmp_path / "model", device=None)
    assert inherited._resolve_device(_FakeTorch(cuda_available=True)) is None

    explicit = BgeM3EmbeddingProvider(model_path=tmp_path / "model", device="mps")
    assert explicit._resolve_device(_FakeTorch(cuda_available=False)) == "mps"


def test_local_bge_preserves_legacy_constructor_properties_and_methods(tmp_path: Path) -> None:
    provider = BgeM3EmbeddingProvider(
        model_path=tmp_path / "model",
        batch_size=4,
        max_length=128,
        device=None,
    )
    _install_fake_load(provider)

    query = provider.embed_query("text-3")
    documents = provider.embed_texts(["text-1", "text-2"])

    assert provider.model_name == "BAAI/bge-m3"
    assert provider.dimension == 1024
    assert provider.signature.provider == "local_bge_m3"
    assert provider.signature.pooling == "cls"
    assert query.shape == (1024,)
    assert query.dtype == np.dtype(np.float32)
    assert query[3] == pytest.approx(1.0)
    assert len(documents) == 2
    assert documents[0][1] == pytest.approx(1.0)
    assert documents[1][2] == pytest.approx(1.0)
