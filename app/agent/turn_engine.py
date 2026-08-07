from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.agent.message_store import MessageStore
from app.agent.ports import (
    CharacterProviderPort,
    ContextBuilderPort,
    MemoryReaderPort,
    MemoryWriteClassifierPort,
    MemoryWriterPort,
    StateUpdaterPort,
)
from app.agent.prompt_context import PromptCompiler
from app.character.schemas import CharacterRuntimeContext
from app.config import Settings
from app.errors import map_exception, redact_sensitive
from app.memory.schemas import MemoryQuery, MemorySearchResult
from app.observability.ports import PromptRunLoggerPort, TraceLoggerPort
from app.observability.schemas import ConversationRun, PromptRunRecord, TraceEvent
from app.retrieval.ports import EvidenceProviderPort
from app.schemas import (
    AgentTurnInput,
    AgentTurnOutput,
    MemoryDecision,
    StateUpdate,
    UsedMemory,
)
from app.semantic.disabled import DisabledSemanticQueryCoordinator
from app.semantic.ports import SemanticQueryCoordinatorPort
from app.semantic.schemas import SemanticMessage, SemanticTurnInput
from app.session.ports import SessionStorePort
from app.state.store import RelationshipStore
from app.utils.ids import new_id, utc_now


@dataclass
class TurnPreflight:
    turn_id: str
    turn_input: AgentTurnInput = field(repr=False)
    active_character_id: str
    warnings: list[str]
    run: ConversationRun = field(repr=False)
    streaming_safe_observability: bool = False


@dataclass(frozen=True)
class CharacterResolution:
    active_character_id: str
    warnings: tuple[str, ...]


@dataclass
class PreparedDialogueTurn:
    preflight: TurnPreflight
    user_message_id: str
    prompt_messages: list[dict[str, str]] = field(repr=False)
    prompt_context_metadata: dict[str, Any] = field(repr=False)
    prompt_sections: list[dict[str, Any]] = field(repr=False)
    memory_results: list[MemorySearchResult] = field(repr=False)


@dataclass(frozen=True)
class TurnCompletionPlan:
    state_update: StateUpdate = field(repr=False)
    memory_decision: MemoryDecision = field(repr=False)
    state_delta: dict[str, int]


class DialogueTurnEngine:
    def __init__(
        self,
        *,
        settings: Settings,
        session_store: SessionStorePort,
        message_store: MessageStore,
        relationship_store: RelationshipStore,
        memory_reader: MemoryReaderPort,
        memory_writer: MemoryWriterPort,
        memory_write_classifier: MemoryWriteClassifierPort,
        state_updater: StateUpdaterPort,
        character_provider: CharacterProviderPort,
        evidence_provider: EvidenceProviderPort,
        context_builder: ContextBuilderPort,
        trace_logger: TraceLoggerPort,
        prompt_run_logger: PromptRunLoggerPort,
        semantic_query_coordinator: SemanticQueryCoordinatorPort | None = None,
        prompt_compiler: PromptCompiler | None = None,
    ) -> None:
        self.settings = settings
        self.session_store = session_store
        self.message_store = message_store
        self.relationship_store = relationship_store
        self.memory_reader = memory_reader
        self.memory_writer = memory_writer
        self.memory_write_classifier = memory_write_classifier
        self.state_updater = state_updater
        self.character_provider = character_provider
        self.evidence_provider = evidence_provider
        self.context_builder = context_builder
        self.trace_logger = trace_logger
        self.prompt_run_logger = prompt_run_logger
        self.semantic_query_coordinator = (
            semantic_query_coordinator or DisabledSemanticQueryCoordinator()
        )
        self.prompt_compiler = prompt_compiler or PromptCompiler()

    def preflight(
        self,
        *,
        turn_input: AgentTurnInput,
        turn_id: str,
        record_started: bool = True,
        streaming_safe_observability: bool = False,
        character_resolution: CharacterResolution | None = None,
    ) -> TurnPreflight:
        resolution = character_resolution or self.resolve_character(
            requested_character_id=turn_input.character_id
        )
        warnings = list(resolution.warnings)
        active_character_id = resolution.active_character_id
        try:
            self.session_store.ensure_session(
                session_id=turn_input.session_id,
                user_id=turn_input.user_id,
                character_id=active_character_id,
            )
        except Exception as exc:
            error_info = map_exception(exc)
            self._record_trace(
                event_type="session_validation_failed",
                turn_id=turn_id,
                turn_input=turn_input,
                payload=error_info.to_log_dict(),
                level="error",
                character_id=active_character_id,
            )
            raise
        run = ConversationRun(
            turn_id=turn_id,
            user_id=turn_input.user_id,
            session_id=turn_input.session_id,
            character_id=active_character_id,
        )
        if turn_input.character_id != active_character_id:
            warnings.append("character_id_mismatch_using_loaded_profile")
        preflight = TurnPreflight(
            turn_id=turn_id,
            turn_input=turn_input,
            active_character_id=active_character_id,
            warnings=warnings,
            run=run,
            streaming_safe_observability=streaming_safe_observability,
        )
        if record_started:
            self.record_started(preflight=preflight)
        return preflight

    def resolve_character(self, *, requested_character_id: str) -> CharacterResolution:
        warnings: list[str] = []
        try:
            character_profile = self.character_provider.load_profile(
                character_id=requested_character_id,
            )
        except KeyError:
            character_profile = self.character_provider.load_profile(
                character_id=self.settings.default_character_id,
            )
            warnings.append("character_id_unknown_using_default_profile")
        return CharacterResolution(
            active_character_id=character_profile.character_id,
            warnings=tuple(warnings),
        )

    def record_started(self, *, preflight: TurnPreflight) -> None:
        payload: dict[str, Any] = {
            "requested_character_id": preflight.turn_input.character_id,
            "active_character_id": preflight.active_character_id,
        }
        if not preflight.streaming_safe_observability:
            payload["metadata"] = preflight.turn_input.metadata
        self._record_trace(
            event_type="turn_started",
            turn_id=preflight.turn_id,
            turn_input=preflight.turn_input,
            payload=payload,
            character_id=preflight.active_character_id,
        )

    def prepare(
        self,
        *,
        preflight: TurnPreflight,
        user_message_id: str,
        character_context: CharacterRuntimeContext | None = None,
    ) -> PreparedDialogueTurn:
        turn_input = preflight.turn_input
        active_character_id = preflight.active_character_id
        turn_id = preflight.turn_id
        character_context = character_context or self.prepare_character_context(
            preflight=preflight
        )
        recent_messages = self.message_store.get_recent_messages(
            session_id=turn_input.session_id,
            limit=self.settings.recent_message_limit,
        )
        recent_messages = [
            message for message in recent_messages if message.id != user_message_id
        ]
        relationship_state = self.relationship_store.get_or_create_relationship_state(
            user_id=turn_input.user_id,
            character_id=active_character_id,
        )
        semantic_messages: list[SemanticMessage] = []
        for message in recent_messages:
            if message.role == "user":
                semantic_role = "user"
            elif message.role == "assistant":
                semantic_role = "assistant"
            else:
                continue
            semantic_messages.append(
                SemanticMessage(
                    id=message.id,
                    role=semantic_role,
                    content=message.content,
                )
            )
        semantic_context = self.semantic_query_coordinator.prepare_turn_context(
            turn_input=SemanticTurnInput(
                turn_id=turn_id,
                user_id=turn_input.user_id,
                session_id=turn_input.session_id,
                character_id=active_character_id,
                current_user_message_id=user_message_id,
                current_input=turn_input.message,
                recent_messages=tuple(semantic_messages),
            )
        )
        self._record_trace(
            event_type="semantic_context_prepared",
            turn_id=turn_id,
            turn_input=turn_input,
            payload=semantic_context.safe_summary(),
            character_id=active_character_id,
            level="warning" if semantic_context.status == "failed" else "info",
        )
        memory_results = self.memory_reader.retrieve_query(
            memory_query=MemoryQuery(
                user_id=turn_input.user_id,
                query_text=turn_input.message,
                character_id=active_character_id,
                session_id=turn_input.session_id,
                limit=6,
            ),
            semantic_context=semantic_context,
        )
        retrieved_memories = [result.record for result in memory_results]
        evidence_pack = self.evidence_provider.build_evidence_pack(
            user_id=turn_input.user_id,
            character_id=active_character_id,
            query=turn_input.message,
            metadata=turn_input.metadata,
            semantic_context=semantic_context,
        )
        self._record_trace(
            event_type="context_sources_selected",
            turn_id=turn_id,
            turn_input=turn_input,
            payload={
                "memory_results": [
                    {
                        "id": result.record.id,
                        "score": result.score,
                        "match_reason": result.match_reason,
                    }
                    for result in memory_results
                ],
                "lore_evidence_ids": [item.id for item in evidence_pack.lore_results],
                "voice_evidence_ids": [item.id for item in evidence_pack.voice_results],
            },
            character_id=active_character_id,
        )
        prompt_context = self.context_builder.build_context(
            character_context=character_context,
            user_input=turn_input.message,
            recent_messages=recent_messages,
            long_term_memories=retrieved_memories,
            relationship_state=relationship_state,
            evidence_pack=evidence_pack,
            metadata=turn_input.metadata,
        )
        prompt_context_metadata = prompt_context.metadata
        prompt_sections = self._serialize_prompt_sections(prompt_context)
        context_budget = prompt_context.metadata.get("context_budget")
        if context_budget and context_budget.get("budget_enabled"):
            self._record_trace(
                event_type="context_budget_applied",
                turn_id=turn_id,
                turn_input=turn_input,
                payload=context_budget,
                level=(
                    "warning"
                    if context_budget.get("status") == "protected_floor_exceeded"
                    else "info"
                ),
                character_id=active_character_id,
            )
        prompt_trace_payload: dict[str, Any] = {
            "sections": [
                {
                    "id": section.id,
                    "source_ids": section.source_ids,
                    "line_count": len(section.content_lines),
                }
                for section in prompt_context.sections
            ],
        }
        if preflight.streaming_safe_observability:
            prompt_trace_payload["metadata"] = self._stream_prompt_metadata_summary(
                prompt_context.metadata
            )
        else:
            prompt_trace_payload["metadata"] = prompt_context.metadata
        self._record_trace(
            event_type="prompt_context_built",
            turn_id=turn_id,
            turn_input=turn_input,
            payload=prompt_trace_payload,
            character_id=active_character_id,
        )
        prompt_messages = self.prompt_compiler.compile_messages(context=prompt_context)
        return PreparedDialogueTurn(
            preflight=preflight,
            user_message_id=user_message_id,
            prompt_messages=prompt_messages,
            prompt_context_metadata=prompt_context_metadata,
            prompt_sections=prompt_sections,
            memory_results=memory_results,
        )

    def prepare_character_context(
        self,
        *,
        preflight: TurnPreflight,
    ) -> CharacterRuntimeContext:
        turn_input = preflight.turn_input
        active_character_id = preflight.active_character_id
        turn_id = preflight.turn_id
        if turn_input.character_id != active_character_id:
            self._record_trace(
                event_type="character_id_fallback",
                turn_id=turn_id,
                turn_input=turn_input,
                payload={
                    "requested_character_id": turn_input.character_id,
                    "active_character_id": active_character_id,
                },
                level="warning",
                character_id=active_character_id,
            )
        return self.character_provider.build_runtime_context(
            character_id=active_character_id,
            user_input=turn_input.message,
            metadata=turn_input.metadata,
        )

    def record_response_generated(
        self,
        *,
        preflight: TurnPreflight,
        response: str,
    ) -> None:
        self._record_trace(
            event_type="response_generated",
            turn_id=preflight.turn_id,
            turn_input=preflight.turn_input,
            payload={"response_length": len(response)},
            character_id=preflight.active_character_id,
        )

    def record_response_stream_started(self, *, preflight: TurnPreflight) -> None:
        self._record_trace(
            event_type="response_stream_started",
            turn_id=preflight.turn_id,
            turn_input=preflight.turn_input,
            payload={"status": "started"},
            character_id=preflight.active_character_id,
        )

    def record_stream_completed(
        self,
        *,
        prepared: PreparedDialogueTurn,
        warnings: tuple[str, ...],
        delta_count: int,
        response_char_count: int,
        response_utf8_bytes: int,
    ) -> None:
        preflight = prepared.preflight
        preflight.run.ended_at = utc_now()
        preflight.run.status = "completed"
        preflight.run.warnings = list(warnings)
        summary = {
            "status": "completed",
            "delta_count": delta_count,
            "response_char_count": response_char_count,
            "response_utf8_bytes": response_utf8_bytes,
        }
        try:
            self._record_trace(
                event_type="response_stream_completed",
                turn_id=preflight.turn_id,
                turn_input=preflight.turn_input,
                payload=summary,
                character_id=preflight.active_character_id,
            )
            self._record_trace(
                event_type="turn_completed",
                turn_id=preflight.turn_id,
                turn_input=preflight.turn_input,
                payload={**summary, "warnings": list(warnings)},
                character_id=preflight.active_character_id,
            )
        except Exception:
            pass
        self._record_stream_prompt_summary_best_effort(
            prepared=prepared,
            status="completed",
            summary=summary,
            warnings=warnings,
        )

    def record_stream_failed(
        self,
        *,
        preflight: TurnPreflight,
        prepared: PreparedDialogueTurn | None,
        error: Exception,
        terminal_status: str,
        delta_count: int,
        response_char_count: int,
        response_utf8_bytes: int,
    ) -> None:
        error_info = map_exception(error)
        preflight.run.ended_at = utc_now()
        preflight.run.status = "failed"
        summary = {
            "status": terminal_status,
            "delta_count": delta_count,
            "response_char_count": response_char_count,
            "response_utf8_bytes": response_utf8_bytes,
            "error_code": error_info.code.value,
        }
        try:
            self._record_trace(
                event_type="response_stream_failed",
                turn_id=preflight.turn_id,
                turn_input=preflight.turn_input,
                payload=summary,
                level="error",
                character_id=preflight.active_character_id,
            )
            self._record_trace(
                event_type="turn_failed",
                turn_id=preflight.turn_id,
                turn_input=preflight.turn_input,
                payload=summary,
                level="error",
                character_id=preflight.active_character_id,
            )
        except Exception:
            pass
        if prepared is not None:
            self._record_stream_prompt_summary_best_effort(
                prepared=prepared,
                status="failed",
                summary=summary,
                warnings=tuple(preflight.warnings),
                error_code=error_info.code.value,
                error_type=error_info.error_type,
                error_public_message=error_info.public_message,
            )

    def decide_state_update(
        self,
        *,
        prepared: PreparedDialogueTurn,
        assistant_response: str,
    ) -> StateUpdate:
        preflight = prepared.preflight
        state_update = self.state_updater.decide(
            user_input=preflight.turn_input.message,
            assistant_response=assistant_response,
        )
        self._record_trace(
            event_type="state_update_decided",
            turn_id=preflight.turn_id,
            turn_input=preflight.turn_input,
            payload={
                "user_emotion": state_update.user_emotion,
                "aemeath_mood": state_update.aemeath_mood,
                "trust_delta": state_update.trust_delta,
                "affection_delta": state_update.affection_delta,
                "familiarity_delta": state_update.familiarity_delta,
                "reason": state_update.reason,
            },
            character_id=preflight.active_character_id,
        )
        return state_update

    def decide_memory_write(
        self,
        *,
        prepared: PreparedDialogueTurn,
        assistant_response: str,
    ) -> MemoryDecision:
        return self.memory_write_classifier.decide(
            user_input=prepared.preflight.turn_input.message,
            assistant_response=assistant_response,
        )

    def build_completion_plan(
        self,
        *,
        state_update: StateUpdate,
        memory_decision: MemoryDecision,
    ) -> TurnCompletionPlan:
        return TurnCompletionPlan(
            state_update=state_update,
            memory_decision=memory_decision,
            state_delta={
                "trust": state_update.trust_delta,
                "affection": state_update.affection_delta,
                "familiarity": state_update.familiarity_delta,
            },
        )

    def decide_completion(
        self,
        *,
        prepared: PreparedDialogueTurn,
        assistant_response: str,
    ) -> TurnCompletionPlan:
        state_update = self.decide_state_update(
            prepared=prepared,
            assistant_response=assistant_response,
        )
        memory_decision = self.decide_memory_write(
            prepared=prepared,
            assistant_response=assistant_response,
        )
        return self.build_completion_plan(
            state_update=state_update,
            memory_decision=memory_decision,
        )

    def record_memory_write_decided(
        self,
        *,
        prepared: PreparedDialogueTurn,
        memory_decision: MemoryDecision,
        created_memory_id: str | None,
        window_memory_job_id: str | None,
    ) -> None:
        preflight = prepared.preflight
        self._record_trace(
            event_type="memory_write_decided",
            turn_id=preflight.turn_id,
            turn_input=preflight.turn_input,
            payload={
                "should_write": memory_decision.should_write,
                "operation": memory_decision.operation,
                "created_memory_id": created_memory_id,
                "window_memory_job_id": window_memory_job_id,
                "reason": memory_decision.reason,
            },
            character_id=preflight.active_character_id,
        )

    def record_completed(
        self,
        *,
        prepared: PreparedDialogueTurn,
        response: str,
    ) -> None:
        preflight = prepared.preflight
        preflight.run.ended_at = utc_now()
        preflight.run.status = "completed"
        preflight.run.warnings = preflight.warnings
        self._record_trace(
            event_type="turn_completed",
            turn_id=preflight.turn_id,
            turn_input=preflight.turn_input,
            payload={
                "status": preflight.run.status,
                "started_at": preflight.run.started_at,
                "ended_at": preflight.run.ended_at,
                "warnings": preflight.warnings,
            },
            character_id=preflight.active_character_id,
        )
        self._record_prompt_run(
            prepared=prepared,
            response=response,
            status="completed",
        )

    def record_failed(
        self,
        *,
        preflight: TurnPreflight,
        prepared: PreparedDialogueTurn | None,
        response: str | None,
        error: Exception,
    ) -> None:
        error_info = map_exception(error)
        log_error = error_info.to_log_dict()
        preflight.run.ended_at = utc_now()
        preflight.run.status = "failed"
        preflight.run.warnings = preflight.warnings
        self._record_trace(
            event_type="turn_failed",
            turn_id=preflight.turn_id,
            turn_input=preflight.turn_input,
            payload={
                "status": preflight.run.status,
                "started_at": preflight.run.started_at,
                "ended_at": preflight.run.ended_at,
                "warnings": preflight.warnings,
                "error": log_error,
            },
            level="error",
            character_id=preflight.active_character_id,
        )
        if prepared is not None:
            self._record_prompt_run(
                prepared=prepared,
                response=response,
                status="failed",
                error_code=error_info.code.value,
                error_type=error_info.error_type,
                error_public_message=error_info.public_message,
                error=log_error["internal_message"],
            )

    def build_output(
        self,
        *,
        prepared: PreparedDialogueTurn,
        response: str,
        completion_plan: TurnCompletionPlan,
    ) -> AgentTurnOutput:
        preflight = prepared.preflight
        return AgentTurnOutput(
            turn_id=preflight.turn_id,
            session_id=preflight.turn_input.session_id,
            response=response,
            used_memory_ids=[result.record.id for result in prepared.memory_results],
            state_delta=completion_plan.state_delta,
            warnings=preflight.warnings,
            used_memories=[
                UsedMemory(
                    id=result.record.id,
                    content=result.record.content,
                    score=result.score,
                    match_reason=result.match_reason,
                )
                for result in prepared.memory_results
            ],
        )

    def _safe_trace_payload(self, value: Any) -> Any:
        return redact_sensitive(value)

    def _record_trace(
        self,
        *,
        event_type: str,
        turn_id: str,
        turn_input: AgentTurnInput,
        payload: dict[str, Any],
        level: str = "info",
        character_id: str | None = None,
    ) -> None:
        self.trace_logger.record(
            TraceEvent(
                event_type=event_type,
                turn_id=turn_id,
                session_id=turn_input.session_id,
                user_id=turn_input.user_id,
                character_id=character_id or turn_input.character_id,
                payload=self._safe_trace_payload(payload),
                level=level,
            )
        )

    def _serialize_prompt_sections(self, prompt_context: Any) -> list[dict[str, Any]]:
        return [
            {
                "id": section.id,
                "title": section.title,
                "content_lines": section.content_lines,
                "source_ids": section.source_ids,
                "metadata": self._safe_trace_payload(section.metadata),
            }
            for section in prompt_context.sections
        ]

    def _record_prompt_run(
        self,
        *,
        prepared: PreparedDialogueTurn,
        response: str | None,
        status: str,
        error_code: str | None = None,
        error_type: str | None = None,
        error_public_message: str | None = None,
        error: str | None = None,
    ) -> None:
        preflight = prepared.preflight
        self.prompt_run_logger.record(
            PromptRunRecord(
                run_id=new_id("prompt_run"),
                turn_id=preflight.turn_id,
                session_id=preflight.turn_input.session_id,
                user_id=preflight.turn_input.user_id,
                character_id=preflight.active_character_id,
                model=self.settings.aemeath_model,
                messages=self._safe_trace_payload(prepared.prompt_messages),
                prompt_context_metadata=self._safe_trace_payload(
                    prepared.prompt_context_metadata
                ),
                sections=self._safe_trace_payload(prepared.prompt_sections),
                response=response,
                warnings=list(preflight.warnings),
                status=status,
                error_code=error_code,
                error_type=error_type,
                error_public_message=error_public_message,
                error=error,
            )
        )

    def _record_stream_prompt_summary_best_effort(
        self,
        *,
        prepared: PreparedDialogueTurn,
        status: str,
        summary: dict[str, Any],
        warnings: tuple[str, ...],
        error_code: str | None = None,
        error_type: str | None = None,
        error_public_message: str | None = None,
    ) -> None:
        preflight = prepared.preflight
        section_summary = [
            {
                "id": section.get("id"),
                "line_count": len(section.get("content_lines", ())),
                "source_count": len(section.get("source_ids", ())),
            }
            for section in prepared.prompt_sections
        ]
        metadata = {
            "streaming_summary": dict(summary),
            "prompt_sections": section_summary,
            **self._stream_prompt_metadata_summary(prepared.prompt_context_metadata),
        }
        try:
            self.prompt_run_logger.record(
                PromptRunRecord(
                    run_id=new_id("prompt_run"),
                    turn_id=preflight.turn_id,
                    session_id=preflight.turn_input.session_id,
                    user_id=preflight.turn_input.user_id,
                    character_id=preflight.active_character_id,
                    model=self.settings.aemeath_model,
                    messages=[],
                    prompt_context_metadata=metadata,
                    sections=[],
                    response=None,
                    warnings=list(warnings),
                    status="completed" if status == "completed" else "failed",
                    error_code=error_code,
                    error_type=error_type,
                    error_public_message=error_public_message,
                    error=None,
                )
            )
        except Exception:
            return

    def _stream_prompt_metadata_summary(self, metadata: dict[str, Any]) -> dict[str, Any]:
        if not self.settings.enable_context_budget:
            return {}
        budget = metadata.get("context_budget")
        if not isinstance(budget, dict):
            return {}
        safe_budget: dict[str, Any] = {}
        allowed_numeric = {
            "max_chars",
            "estimated_before",
            "estimated_after",
            "over_budget_chars",
            "dropped_line_count",
        }
        for key in allowed_numeric:
            value = budget.get(key)
            if isinstance(value, bool) or (
                isinstance(value, int) and not isinstance(value, bool)
            ):
                safe_budget[key] = value
        status = budget.get("status")
        if status in {"disabled", "protected_floor_exceeded", "trimmed", "within_budget"}:
            safe_budget["status"] = status
        if isinstance(budget.get("budget_enabled"), bool):
            safe_budget["budget_enabled"] = budget["budget_enabled"]
        return {"context_budget": safe_budget}
