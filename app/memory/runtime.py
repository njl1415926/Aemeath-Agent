from __future__ import annotations

import warnings

from app.config import Settings
from app.embeddings.config import load_embedding_space_profile
from app.embeddings.errors import EmbeddingConfigurationError
from app.embeddings.factory import create_embedding_provider
from app.embeddings.runtime import EmbeddingProviderRuntime
from app.embeddings.schemas import EmbeddingRuntimeOptions
from app.observability.ports import AppLoggerPort

_LEGACY_PROFILE_MAP = {
    "hash": "deterministic_hash_v1",
    "bge-m3": "local_bge_m3_v1",
    "bge_m3": "local_bge_m3_v1",
    "bge": "local_bge_m3_v1",
}


def build_memory_embedding_runtime(
    settings: Settings,
    *,
    app_logger: AppLoggerPort | None = None,
) -> EmbeddingProviderRuntime:
    profile_id = settings.embedding_profile.strip() if settings.embedding_profile else None
    legacy_provider = (
        settings.memory_embedding_provider.strip().lower()
        if settings.memory_embedding_provider
        else None
    )
    mapped_legacy_profile = (
        _LEGACY_PROFILE_MAP.get(legacy_provider) if legacy_provider is not None else None
    )
    if legacy_provider is not None and mapped_legacy_profile is None:
        raise EmbeddingConfigurationError("legacy_provider_invalid")
    if profile_id is not None and legacy_provider is not None:
        if mapped_legacy_profile != profile_id:
            raise EmbeddingConfigurationError("canonical_legacy_profile_conflict")
        warnings.warn(
            "AEMEATH_MEMORY_EMBEDDING_PROVIDER is redundant with the selected "
            "Foundation profile and will be removed after the compatibility release.",
            DeprecationWarning,
            stacklevel=2,
        )
    if profile_id is None:
        profile_id = mapped_legacy_profile
        if profile_id is not None:
            warnings.warn(
                "AEMEATH_MEMORY_EMBEDDING_PROVIDER is deprecated; use the approved "
                "AEMEATH_EMBEDDING_PROFILE instead.",
                DeprecationWarning,
                stacklevel=2,
            )
    if profile_id is None:
        raise EmbeddingConfigurationError("embedding_profile_not_configured")
    profile = load_embedding_space_profile(profile_id)
    allow_legacy_hash = legacy_provider == "hash"
    return create_embedding_provider(
        profile,
        _runtime_options(settings, allow_legacy_hash=allow_legacy_hash),
        app_logger=app_logger,
    )


def build_memory_embedding_provider(
    settings: Settings,
    *,
    app_logger: AppLoggerPort | None = None,
) -> EmbeddingProviderRuntime:
    """Deprecated name retained for one release; it now returns the owned bundle."""
    warnings.warn(
        "build_memory_embedding_provider now returns EmbeddingProviderRuntime; "
        "use build_memory_embedding_runtime.",
        DeprecationWarning,
        stacklevel=2,
    )
    return build_memory_embedding_runtime(settings, app_logger=app_logger)


def _runtime_options(
    settings: Settings,
    *,
    allow_legacy_hash: bool = False,
) -> EmbeddingRuntimeOptions:
    return EmbeddingRuntimeOptions(
        batch_size=settings.embedding_batch_size,
        timeout_seconds=settings.embedding_timeout_seconds,
        max_retries=settings.embedding_max_retries,
        retry_backoff_seconds=settings.embedding_retry_backoff_seconds,
        local_model_path=settings.embedding_local_model_path,
        local_device=settings.embedding_local_device,
        local_max_length=settings.embedding_local_max_length,
        lazy_load=settings.embedding_lazy_load,
        allow_hash=settings.embedding_allow_hash or allow_legacy_hash,
    )
