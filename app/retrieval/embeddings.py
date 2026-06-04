from __future__ import annotations

import hashlib
import math
import re
from pathlib import Path
from typing import Protocol

import numpy as np


class EmbeddingProviderPort(Protocol):
    model_name: str
    dimension: int

    def embed_texts(self, texts: list[str]) -> list[np.ndarray]:
        ...

    def embed_query(self, text: str) -> np.ndarray:
        ...


TOKEN_RE = re.compile(r"[\w]+|[\u4e00-\u9fff]")


class DeterministicHashEmbeddingProvider:
    model_name = "deterministic_hash_v1"

    def __init__(self, *, dimension: int = 384):
        self.dimension = dimension

    def embed_texts(self, texts: list[str]) -> list[np.ndarray]:
        return [self._embed(text) for text in texts]

    def embed_query(self, text: str) -> np.ndarray:
        return self._embed(text)

    def _embed(self, text: str) -> np.ndarray:
        vector = np.zeros(self.dimension, dtype=np.float32)
        tokens = TOKEN_RE.findall(text.lower())
        if not tokens:
            return vector
        for token in tokens:
            digest = hashlib.sha1(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "little") % self.dimension
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vector[index] += sign
        norm = float(np.linalg.norm(vector))
        if norm > 0:
            vector /= norm
        return vector


class BgeM3EmbeddingProvider:
    model_name = "BAAI/bge-m3"
    dimension = 1024

    def __init__(
        self,
        *,
        model_path: Path,
        batch_size: int = 8,
        max_length: int = 8192,
        device: str | None = None,
    ):
        self.model_path = model_path
        self.batch_size = batch_size
        self.max_length = max_length
        self.device = device
        self._tokenizer = None
        self._model = None

    def embed_texts(self, texts: list[str]) -> list[np.ndarray]:
        if not texts:
            return []
        tokenizer, model, torch = self._load()
        embeddings: list[np.ndarray] = []
        for start in range(0, len(texts), self.batch_size):
            batch = texts[start:start + self.batch_size]
            encoded = tokenizer(
                batch,
                padding=True,
                truncation=True,
                max_length=self.max_length,
                return_tensors="pt",
            )
            encoded = {key: value.to(model.device) for key, value in encoded.items()}
            with torch.no_grad():
                output = model(**encoded)
            batch_embeddings = output.last_hidden_state[:, 0]
            batch_embeddings = torch.nn.functional.normalize(batch_embeddings, p=2, dim=1)
            embeddings.extend(
                item.detach().cpu().numpy().astype(np.float32)
                for item in batch_embeddings
            )
        return embeddings

    def embed_query(self, text: str) -> np.ndarray:
        return self.embed_texts([text])[0]

    def _load(self):
        if self._tokenizer is not None and self._model is not None:
            import torch

            return self._tokenizer, self._model, torch
        import torch
        from transformers import AutoModel, AutoTokenizer

        tokenizer = AutoTokenizer.from_pretrained(self.model_path, local_files_only=True)
        model = AutoModel.from_pretrained(self.model_path, local_files_only=True)
        if self.device:
            model = model.to(self.device)
        model.eval()
        self._tokenizer = tokenizer
        self._model = model
        return tokenizer, model, torch


def cosine_similarity(left: np.ndarray, right: np.ndarray) -> float:
    left_norm = float(np.linalg.norm(left))
    right_norm = float(np.linalg.norm(right))
    if math.isclose(left_norm, 0.0) or math.isclose(right_norm, 0.0):
        return 0.0
    return float(np.dot(left, right) / (left_norm * right_norm))
