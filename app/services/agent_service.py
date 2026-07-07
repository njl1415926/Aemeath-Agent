from __future__ import annotations

from time import perf_counter

from app.agent.dialogue_controller import DialogueController
from app.errors import map_exception, redact_sensitive
from app.observability.noop import NoopAppLogger
from app.observability.ports import AppLoggerPort
from app.observability.schemas import AppLogRecord
from app.schemas import AgentTurnInput
from app.services.schemas import (
    ChatRequest,
    ChatResponse,
    UsedMemorySummary,
    UserContext,
)


class AgentService:
    def __init__(
        self,
        *,
        controller: DialogueController,
        app_logger: AppLoggerPort | None = None,
    ):
        self.controller = controller
        self.app_logger = app_logger or NoopAppLogger()

    def handle_chat(
        self,
        *,
        user_context: UserContext,
        request: ChatRequest,
    ) -> ChatResponse:
        started_at = perf_counter()
        turn_input = AgentTurnInput(
            user_id=user_context.user_id,
            session_id=request.session_id,
            character_id=request.character_id,
            message=request.message,
            metadata=request.metadata,
        )
        try:
            output = self.controller.handle_user_message(turn_input)
        except Exception as exc:
            error_info = map_exception(exc)
            self._record_app_log(
                event_name="agent_service.chat_failed",
                level="error",
                user_context=user_context,
                request=request,
                status="failed",
                duration_ms=self._duration_ms(started_at),
                error_code=error_info.code.value,
            )
            raise

        response = ChatResponse(
            turn_id=output.turn_id,
            session_id=output.session_id,
            character_id=request.character_id,
            response=output.response,
            warnings=list(output.warnings),
            used_memory_ids=list(output.used_memory_ids),
            state_delta=dict(output.state_delta),
            used_memories=[
                UsedMemorySummary(
                    id=memory.id,
                    content=memory.content,
                    score=memory.score,
                    match_reason=memory.match_reason,
                )
                for memory in output.used_memories
            ],
        )
        self._record_app_log(
            event_name="agent_service.chat_completed",
            level="info",
            user_context=user_context,
            request=request,
            status="completed",
            duration_ms=self._duration_ms(started_at),
            turn_id=response.turn_id,
            warning_count=len(response.warnings),
        )
        return response

    def _record_app_log(
        self,
        *,
        event_name: str,
        level: str,
        user_context: UserContext,
        request: ChatRequest,
        status: str,
        duration_ms: float,
        turn_id: str | None = None,
        error_code: str | None = None,
        warning_count: int | None = None,
    ) -> None:
        payload = {
            "status": status,
            "duration_ms": duration_ms,
        }
        if warning_count is not None:
            payload["warning_count"] = warning_count
        self.app_logger.record(
            AppLogRecord(
                event_name=event_name,
                level=level,
                request_id=user_context.request_id,
                turn_id=turn_id,
                session_id=request.session_id,
                user_id=user_context.user_id,
                character_id=request.character_id,
                error_code=error_code,
                payload=redact_sensitive(payload),
            )
        )

    def _duration_ms(self, started_at: float) -> float:
        return round((perf_counter() - started_at) * 1000, 3)
