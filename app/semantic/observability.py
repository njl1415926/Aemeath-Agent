from __future__ import annotations

from app.observability.ports import AppLoggerPort
from app.observability.schemas import AppLogRecord
from app.semantic.schemas import TurnSemanticContext


def record_semantic_context(
    app_logger: AppLoggerPort,
    *,
    context: TurnSemanticContext,
    user_id: str,
    session_id: str,
    character_id: str,
) -> None:
    event_name = {
        "ready": "semantic.query_completed",
        "skipped": "semantic.query_skipped",
        "failed": "semantic.query_failed",
        "not_requested": "semantic.query_skipped",
    }[context.status]
    error_code = next(
        (
            resolution.error_code
            for resolution in context.resolutions
            if resolution.error_code is not None
        ),
        None,
    )
    try:
        app_logger.record(
            AppLogRecord(
                event_name=event_name,
                level="warning" if context.status == "failed" else "info",
                payload=context.safe_summary(),
                turn_id=context.turn_id,
                user_id=user_id,
                session_id=session_id,
                character_id=character_id,
                error_code=error_code,
            )
        )
    except Exception:
        return None


def record_semantic_failure(
    app_logger: AppLoggerPort,
    *,
    turn_input: object,
    duration_ms: float,
    reason_code: str,
    error_code: str,
    provider: str | None,
    profile_id: str | None,
) -> None:
    safe_identity = {
        "turn_id": getattr(turn_input, "turn_id", None),
        "user_id": getattr(turn_input, "user_id", None),
        "session_id": getattr(turn_input, "session_id", None),
        "character_id": getattr(turn_input, "character_id", None),
    }
    try:
        app_logger.record(
            AppLogRecord(
                event_name="semantic.query_failed",
                level="error",
                payload={
                    "reason_code": reason_code,
                    "provider": provider,
                    "profile_id": profile_id,
                    "duration_ms": round(max(0.0, duration_ms), 3),
                },
                turn_id=safe_identity["turn_id"],
                user_id=safe_identity["user_id"],
                session_id=safe_identity["session_id"],
                character_id=safe_identity["character_id"],
                error_code=error_code,
            )
        )
    except Exception:
        return None
