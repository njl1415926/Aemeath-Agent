from __future__ import annotations

from time import perf_counter

from app.agent.dialogue_controller import DialogueController
from app.errors import ErrorCode, map_exception, redact_sensitive
from app.observability.noop import NoopAppLogger
from app.observability.ports import AppLoggerPort
from app.observability.schemas import AppLogRecord
from app.runtime.guardrails.admission import InProcessDialogueAdmission
from app.runtime.guardrails.input_limits import ChatInputLimitPolicyV01
from app.runtime.guardrails.ports import ChatInputLimitPolicyPort, DialogueAdmissionPort
from app.runtime.guardrails.schemas import ChatInputLimitRequest, DialogueAdmissionRequest
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
        admission: DialogueAdmissionPort | None = None,
        input_limit_policy: ChatInputLimitPolicyPort | None = None,
    ):
        self.controller = controller
        self.app_logger = app_logger or NoopAppLogger()
        self.admission = admission or InProcessDialogueAdmission(
            session_wait_timeout_seconds=5.0,
            dialogue_limit=2,
            dialogue_wait_timeout_seconds=0.25,
        )
        self.input_limit_policy = input_limit_policy or ChatInputLimitPolicyV01(
            message_max_chars=4000,
            message_max_utf8_bytes=12000,
            metadata_max_utf8_bytes=8192,
        )

    def handle_chat(
        self,
        *,
        user_context: UserContext,
        request: ChatRequest,
    ) -> ChatResponse:
        started_at = perf_counter()
        lease = None
        try:
            turn_metadata = self._turn_metadata(user_context=user_context, request=request)
            self.input_limit_policy.validate(
                request=ChatInputLimitRequest(
                    message=request.message,
                    metadata=turn_metadata,
                )
            )
            turn_input = AgentTurnInput(
                user_id=user_context.user_id,
                session_id=request.session_id,
                character_id=request.character_id,
                message=request.message,
                metadata=turn_metadata,
            )
            with self.admission.acquire(
                request=DialogueAdmissionRequest(
                    user_id=user_context.user_id,
                    session_id=request.session_id,
                    request_id=user_context.request_id,
                )
            ) as lease:
                self._record_guardrail(
                    event_name="runtime.dialogue_admitted",
                    level="info",
                    user_context=user_context,
                    request=request,
                    payload={
                        "session_wait_ms": round(lease.session_wait_ms, 3),
                        "dialogue_wait_ms": round(lease.dialogue_wait_ms, 3),
                    },
                )
                output = self.controller.handle_user_message(turn_input)
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
                    session_wait_ms=lease.session_wait_ms,
                    dialogue_wait_ms=lease.dialogue_wait_ms,
                )
                self._record_guardrail(
                    event_name="runtime.dialogue_completed",
                    level="info",
                    user_context=user_context,
                    request=request,
                    turn_id=response.turn_id,
                    payload={
                        "status": "completed",
                        "total_ms": self._duration_ms(started_at),
                    },
                )
                return response
        except Exception as exc:
            error_info = map_exception(exc)
            reason = error_info.metadata.get("limit_reason")
            if reason is None:
                reason = error_info.metadata.get("reason")
            if error_info.code == ErrorCode.RUNTIME_BUSY:
                self._record_guardrail(
                    event_name="runtime.dialogue_rejected",
                    level="warning",
                    user_context=user_context,
                    request=request,
                    payload={"limit_reason": reason},
                    error_code=error_info.code.value,
                )
            elif error_info.code == ErrorCode.INVALID_INPUT and isinstance(reason, str):
                self._record_guardrail(
                    event_name="runtime.input_rejected",
                    level="warning",
                    user_context=user_context,
                    request=request,
                    payload={"limit_reason": reason},
                    error_code=error_info.code.value,
                )
            self._record_app_log(
                event_name="agent_service.chat_failed",
                level="error",
                user_context=user_context,
                request=request,
                status="failed",
                duration_ms=self._duration_ms(started_at),
                error_code=error_info.code.value,
                session_wait_ms=lease.session_wait_ms if lease is not None else None,
                dialogue_wait_ms=lease.dialogue_wait_ms if lease is not None else None,
            )
            raise

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
        session_wait_ms: float | None = None,
        dialogue_wait_ms: float | None = None,
    ) -> None:
        payload = {
            "status": status,
            "duration_ms": duration_ms,
        }
        if warning_count is not None:
            payload["warning_count"] = warning_count
        if session_wait_ms is not None:
            payload["session_wait_ms"] = round(session_wait_ms, 3)
        if dialogue_wait_ms is not None:
            payload["dialogue_wait_ms"] = round(dialogue_wait_ms, 3)
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

    def _record_guardrail(
        self,
        *,
        event_name: str,
        level: str,
        user_context: UserContext,
        request: ChatRequest,
        payload: dict,
        turn_id: str | None = None,
        error_code: str | None = None,
    ) -> None:
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

    def _turn_metadata(
        self,
        *,
        user_context: UserContext,
        request: ChatRequest,
    ) -> dict:
        metadata = dict(request.metadata)
        if user_context.request_id:
            metadata["request_id"] = user_context.request_id
        return metadata
