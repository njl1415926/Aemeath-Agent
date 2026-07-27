from __future__ import annotations

import math
import os
import re
import time
import warnings
from pathlib import Path
from typing import Callable, Protocol

from app.embeddings.config import load_embedding_space_profile
from app.embeddings.errors import EmbeddingConfigurationError, EmbeddingSecretMissingError
from app.embeddings.observability import ObservableEmbeddingProvider
from app.embeddings.ports import EmbeddingProviderPort
from app.embeddings.schemas import (
    EmbeddingRuntimeOptions,
    EmbeddingSpaceProfile,
)
from app.observability.ports import AppLoggerPort

SILICONFLOW_API_KEY_ENV = "SILICONFLOW_API_KEY"
_DEVICE_RE = re.compile(r"^(auto|cpu|mps|cuda(?::[0-9]+)?)$")


class SecretResolver(Protocol):
    def __call__(self, name: str) -> str | None:
        ...


def environment_secret_resolver(name: str) -> str | None:
    if name != SILICONFLOW_API_KEY_ENV:
        raise EmbeddingConfigurationError("secret_name_not_allowed")
    return os.getenv(SILICONFLOW_API_KEY_ENV)


def validate_runtime_options(runtime: EmbeddingRuntimeOptions) -> None:
    if (
        isinstance(runtime.batch_size, bool)
        or not isinstance(runtime.batch_size, int)
        or runtime.batch_size <= 0
    ):
        raise EmbeddingConfigurationError("runtime_batch_size_invalid")
    if (
        isinstance(runtime.timeout_seconds, bool)
        or not isinstance(runtime.timeout_seconds, (int, float))
        or not math.isfinite(float(runtime.timeout_seconds))
        or runtime.timeout_seconds <= 0
    ):
        raise EmbeddingConfigurationError("runtime_timeout_invalid")
    if (
        isinstance(runtime.max_retries, bool)
        or not isinstance(runtime.max_retries, int)
        or runtime.max_retries < 0
    ):
        raise EmbeddingConfigurationError("runtime_retries_invalid")
    if (
        isinstance(runtime.retry_backoff_seconds, bool)
        or not isinstance(runtime.retry_backoff_seconds, (int, float))
        or not math.isfinite(float(runtime.retry_backoff_seconds))
        or runtime.retry_backoff_seconds < 0
    ):
        raise EmbeddingConfigurationError("runtime_retry_backoff_invalid")
    if (
        isinstance(runtime.local_max_length, bool)
        or not isinstance(runtime.local_max_length, int)
        or runtime.local_max_length <= 0
    ):
        raise EmbeddingConfigurationError("runtime_local_max_length_invalid")
    if (
        not isinstance(runtime.local_device, str)
        or runtime.local_device != runtime.local_device.strip()
        or not _DEVICE_RE.fullmatch(runtime.local_device.lower())
    ):
        raise EmbeddingConfigurationError("runtime_local_device_invalid")
    if not isinstance(runtime.local_model_path, Path):
        raise EmbeddingConfigurationError("runtime_local_model_path_invalid")
    if not isinstance(runtime.lazy_load, bool) or not isinstance(runtime.allow_hash, bool):
        raise EmbeddingConfigurationError("runtime_boolean_option_invalid")


def create_embedding_provider(
    profile: EmbeddingSpaceProfile,
    runtime: EmbeddingRuntimeOptions,
    *,
    secret_resolver: SecretResolver = environment_secret_resolver,
    app_logger: AppLoggerPort | None = None,
) -> EmbeddingProviderPort:
    """Create an independent runtime provider for an approved vector space."""
    if type(profile) is not EmbeddingSpaceProfile:
        raise EmbeddingConfigurationError("runtime_profile_invalid")
    canonical_profile = load_embedding_space_profile(profile.profile_id)
    if profile != canonical_profile:
        raise EmbeddingConfigurationError("runtime_profile_not_canonical")
    provider = _create_provider(
        canonical_profile,
        runtime,
        secret_resolver=secret_resolver,
    )
    return ObservableEmbeddingProvider(provider, app_logger=app_logger)


def create_qualification_provider(
    profile: EmbeddingSpaceProfile,
    runtime: EmbeddingRuntimeOptions,
    *,
    secret_resolver: SecretResolver = environment_secret_resolver,
    app_logger: AppLoggerPort | None = None,
    http_client: object | None = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> EmbeddingProviderPort:
    """Explicit probe-only path; this is the sole Factory entry that accepts review."""
    provider = _create_provider(
        profile,
        runtime,
        secret_resolver=secret_resolver,
        http_client=http_client,
        sleeper=sleeper,
    )
    return ObservableEmbeddingProvider(provider, app_logger=app_logger)


def _create_provider(
    profile: EmbeddingSpaceProfile,
    runtime: EmbeddingRuntimeOptions,
    *,
    secret_resolver: SecretResolver,
    http_client: object | None = None,
    sleeper: Callable[[float], None] = time.sleep,
):
    validate_runtime_options(runtime)
    if profile.provider == "siliconflow":
        from app.embeddings.providers.siliconflow import SiliconFlowEmbeddingProvider

        api_key = secret_resolver(SILICONFLOW_API_KEY_ENV)
        if not isinstance(api_key, str) or not api_key.strip():
            raise EmbeddingSecretMissingError()
        return SiliconFlowEmbeddingProvider(
            profile=profile,
            runtime=runtime,
            api_key=api_key,
            client=http_client,
            sleeper=sleeper,
        )
    if profile.provider == "local_bge_m3":
        from app.embeddings.providers.local_bge_m3 import BgeM3EmbeddingProvider

        provider = BgeM3EmbeddingProvider(
            model_path=runtime.local_model_path,
            batch_size=runtime.batch_size,
            max_length=runtime.local_max_length,
            device=runtime.local_device,
            profile=profile,
        )
        if not runtime.lazy_load:
            provider._load()
        return provider
    if profile.provider == "deterministic_hash":
        if not runtime.allow_hash:
            raise EmbeddingConfigurationError("hash_provider_not_explicitly_allowed")
        from app.embeddings.providers.hash import DeterministicHashEmbeddingProvider

        return DeterministicHashEmbeddingProvider(
            dimension=profile.dimension,
            profile=profile,
        )
    raise EmbeddingConfigurationError("provider_not_supported")


def create_legacy_embedding_provider(
    provider_name: str,
    *,
    model_path: Path,
    app_logger: AppLoggerPort | None = None,
) -> EmbeddingProviderPort:
    """One-release bridge for AEMEATH_MEMORY_EMBEDDING_* configuration."""
    normalized = provider_name.strip().lower()
    warnings.warn(
        "AEMEATH_MEMORY_EMBEDDING_* is deprecated; migrate to an approved "
        "AEMEATH_EMBEDDING_PROFILE.",
        DeprecationWarning,
        stacklevel=2,
    )
    if normalized == "hash":
        from app.embeddings.providers.hash import DeterministicHashEmbeddingProvider

        provider = DeterministicHashEmbeddingProvider()
    elif normalized in {"bge-m3", "bge_m3", "bge"}:
        from app.embeddings.providers.local_bge_m3 import BgeM3EmbeddingProvider

        provider = BgeM3EmbeddingProvider(model_path=model_path)
    else:
        raise EmbeddingConfigurationError("legacy_provider_invalid")
    return ObservableEmbeddingProvider(provider, app_logger=app_logger)
