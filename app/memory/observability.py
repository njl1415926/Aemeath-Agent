from __future__ import annotations

from typing import Literal

from app.observability.ports import AppLoggerPort
from app.observability.schemas import AppLogRecord

_SAFE_PAYLOAD_FIELDS = {
    "artifact_written_count",
    "compatible_memory_count",
    "coverage_complete",
    "created_memory_count",
    "eligible_memory_count",
    "embedding_pending_count",
    "embedding_status",
    "existing_memory_count",
    "fallback_strategy",
    "gap_count",
    "gap_reason_counts",
    "keyword_result_count",
    "profile_id",
    "provider",
    "repair_delay_seconds",
    "scanned_memory_count",
    "semantic_role",
    "semantic_usage_reason",
    "semantic_vector_consumed",
    "trigger_type",
    "vector_result_count",
}


def record_memory_event(
    app_logger: AppLoggerPort,
    *,
    event_name: str,
    payload: dict[str, object],
    level: Literal["debug", "info", "warning", "error"] = "info",
    error_code: str | None = None,
    user_id: str | None = None,
    session_id: str | None = None,
    character_id: str | None = None,
    turn_id: str | None = None,
) -> None:
    """Record a content-free Memory summary without affecting business outcomes."""

    safe_payload = {
        key: value for key, value in payload.items() if key in _SAFE_PAYLOAD_FIELDS
    }
    try:
        app_logger.record(
            AppLogRecord(
                event_name=event_name,
                level=level,
                payload=safe_payload,
                user_id=user_id,
                session_id=session_id,
                character_id=character_id,
                turn_id=turn_id,
                error_code=error_code,
            )
        )
    except Exception:
        return None
