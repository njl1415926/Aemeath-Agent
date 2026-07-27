from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from app.embeddings.errors import (
    EmbeddingConfigurationError,
    EmbeddingDependencyError,
    EmbeddingInvalidResponseError,
    EmbeddingUnavailableError,
)
from app.embeddings.providers.base import BaseEmbeddingProvider
from app.embeddings.schemas import (
    EmbeddingBatchResult,
    EmbeddingCallDiagnostics,
    EmbeddingModelSignature,
    EmbeddingRequest,
    EmbeddingSpaceProfile,
    EmbeddingUsage,
)
from app.embeddings.validation import (
    empty_vectors,
    normalize_and_validate_vectors,
    validate_request,
)


def _direct_local_signature() -> EmbeddingModelSignature:
    return EmbeddingModelSignature(
        schema_version="embedding-model-signature/v1",
        provider="local_bge_m3",
        model_id="BAAI/bge-m3",
        dimension=1024,
        model_revision="provider_managed",
        pooling="cls",
        normalization="l2",
        query_instruction="",
        document_instruction="",
        preprocessing_version="aemeath-embedding-pre-v1",
        postprocessing_version="normalized-float32-v1",
        profile_id="local_bge_m3_v1",
        canary_set_version="bge_m3_canary_v1",
        expected_fingerprint=None,
    )


class BgeM3EmbeddingProvider(BaseEmbeddingProvider):
    """Lazy local BGE-M3 provider using the model's configured CLS pooling."""

    def __init__(
        self,
        *,
        model_path: Path,
        batch_size: int = 8,
        max_length: int = 8192,
        device: str | None = None,
        profile: EmbeddingSpaceProfile | None = None,
    ):
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size <= 0:
            raise EmbeddingConfigurationError("local_batch_size_invalid")
        if isinstance(max_length, bool) or not isinstance(max_length, int) or max_length <= 0:
            raise EmbeddingConfigurationError("local_max_length_invalid")
        if device is not None and (not isinstance(device, str) or not device.strip()):
            raise EmbeddingConfigurationError("local_device_invalid")
        signature = profile.signature if profile is not None else _direct_local_signature()
        if (
            signature.provider != "local_bge_m3"
            or signature.model_id != "BAAI/bge-m3"
            or signature.dimension != 1024
            or signature.model_revision != "provider_managed"
            or signature.pooling != "cls"
            or signature.normalization != "l2"
            or signature.query_instruction
            or signature.document_instruction
        ):
            raise EmbeddingConfigurationError("local_profile_mismatch")
        super().__init__(signature=signature, legacy_model_name="BAAI/bge-m3")
        self.model_path = Path(model_path)
        self.batch_size = batch_size
        self.max_length = max_length
        self.device = device
        self._tokenizer: Any | None = None
        self._model: Any | None = None
        self._torch: Any | None = None

    def embed(self, request: EmbeddingRequest) -> EmbeddingBatchResult:
        validate_request(request)
        if not request.texts:
            return EmbeddingBatchResult(
                vectors=empty_vectors(self.dimension),
                signature=self.signature,
                usage=EmbeddingUsage(input_count=0, input_tokens=None),
                diagnostics=EmbeddingCallDiagnostics(),
            )
        tokenizer, model, torch = self._load()
        raw_batches: list[np.ndarray] = []
        try:
            for start in range(0, len(request.texts), self.batch_size):
                batch = list(request.texts[start:start + self.batch_size])
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
                cls_embeddings = output.last_hidden_state[:, 0]
                array = cls_embeddings.detach().cpu().numpy()
                raw_batches.append(np.asarray(array))
        except EmbeddingInvalidResponseError:
            raise
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise EmbeddingInvalidResponseError("local_model_output_invalid") from exc
        except Exception as exc:
            raise EmbeddingUnavailableError(reason="local_inference_failed") from exc
        try:
            raw = np.concatenate(raw_batches, axis=0)
        except (TypeError, ValueError) as exc:
            raise EmbeddingInvalidResponseError("local_model_output_shape") from exc
        vectors = normalize_and_validate_vectors(
            raw,
            input_count=len(request.texts),
            dimension=self.dimension,
            reason_prefix="local_model_output",
        )
        return EmbeddingBatchResult(
            vectors=vectors,
            signature=self.signature,
            usage=EmbeddingUsage(input_count=len(request.texts), input_tokens=None),
            diagnostics=EmbeddingCallDiagnostics(),
        )

    def _load(self):
        if self._tokenizer is not None and self._model is not None and self._torch is not None:
            return self._tokenizer, self._model, self._torch
        try:
            import torch
            from transformers import AutoModel, AutoTokenizer
        except (ImportError, ModuleNotFoundError) as exc:
            raise EmbeddingDependencyError("embedding_local_extra_missing") from exc
        try:
            tokenizer = AutoTokenizer.from_pretrained(self.model_path, local_files_only=True)
            model = AutoModel.from_pretrained(self.model_path, local_files_only=True)
            resolved_device = self._resolve_device(torch)
            if resolved_device is not None:
                model = model.to(resolved_device)
            model.eval()
        except Exception as exc:
            raise EmbeddingConfigurationError("local_model_load_failed") from exc
        self._tokenizer = tokenizer
        self._model = model
        self._torch = torch
        return tokenizer, model, torch

    def _resolve_device(self, torch: Any) -> str | None:
        if self.device is None:
            return None
        configured = self.device.strip().lower()
        if configured == "auto":
            return "cuda" if bool(torch.cuda.is_available()) else "cpu"
        return self.device
