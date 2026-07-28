from __future__ import annotations

from typing import Any

from app.agent.message_store import MessageStore
from app.agent.ports import (
    CharacterProviderPort,
    ContextBuilderPort,
    MemoryReaderPort,
    MemoryWriteClassifierPort,
    MemoryWriterPort,
    ResponseGeneratorPort,
    StateUpdaterPort,
)
from app.agent.prompt_context import PromptCompiler
from app.config import Settings
from app.errors import map_exception, redact_sensitive
from app.memory.schemas import MemoryQuery
from app.observability.ports import PromptRunLoggerPort, TraceLoggerPort
from app.observability.schemas import ConversationRun, PromptRunRecord, TraceEvent
from app.retrieval.ports import EvidenceProviderPort
from app.schemas import AgentTurnInput, AgentTurnOutput, UsedMemory
from app.semantic.disabled import DisabledSemanticQueryCoordinator
from app.semantic.ports import SemanticQueryCoordinatorPort
from app.semantic.schemas import SemanticMessage, SemanticTurnInput
from app.session.ports import SessionStorePort
from app.state.store import RelationshipStore
from app.utils.ids import new_id, utc_now


class DialogueController:
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
        response_generator: ResponseGeneratorPort,
        trace_logger: TraceLoggerPort,
        prompt_run_logger: PromptRunLoggerPort,
        semantic_query_coordinator: SemanticQueryCoordinatorPort | None = None,
    ):
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
        self.response_generator = response_generator
        self.trace_logger = trace_logger
        self.prompt_run_logger = prompt_run_logger
        self.semantic_query_coordinator = (
            semantic_query_coordinator or DisabledSemanticQueryCoordinator()
        )
        self.prompt_compiler = PromptCompiler()

    def _safe_trace_payload(self, value: Any) -> Any:
        return redact_sensitive(value)

    def _record_trace(
        self,
        *,
        event_type: str,
        turn_id: str,
        turn_input: AgentTurnInput,
        payload: dict,
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

    def _serialize_prompt_sections(self, prompt_context) -> list[dict[str, Any]]:
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
        turn_id: str,
        turn_input: AgentTurnInput,
        active_character_id: str,
        prompt_messages: list[dict[str, str]],
        prompt_context_metadata: dict[str, Any],
        sections: list[dict[str, Any]],
        response: str | None,
        warnings: list[str],
        status: str,
        error_code: str | None = None,
        error_type: str | None = None,
        error_public_message: str | None = None,
        error: str | None = None,
    ) -> None:
        self.prompt_run_logger.record(
            PromptRunRecord(
                run_id=new_id("prompt_run"),
                turn_id=turn_id,
                session_id=turn_input.session_id,
                user_id=turn_input.user_id,
                character_id=active_character_id,
                model=self.settings.aemeath_model,
                messages=self._safe_trace_payload(prompt_messages),
                prompt_context_metadata=self._safe_trace_payload(prompt_context_metadata),
                sections=self._safe_trace_payload(sections),
                response=response,
                warnings=list(warnings),
                status=status,
                error_code=error_code,
                error_type=error_type,
                error_public_message=error_public_message,
                error=error,
            )
        )

    def handle_user_message(self, turn_input: AgentTurnInput) -> AgentTurnOutput:
        turn_id = new_id("turn")
        warnings: list[str] = []
        prompt_messages: list[dict[str, str]] | None = None
        prompt_context_metadata: dict[str, Any] | None = None
        prompt_sections: list[dict[str, Any]] | None = None
        response: str | None = None
        try:
            character_profile = self.character_provider.load_profile(
                character_id=turn_input.character_id,
            )
        except KeyError:
            character_profile = self.character_provider.load_profile(
                character_id=self.settings.default_character_id,
            )
            warnings.append("character_id_unknown_using_default_profile")
        active_character_id = character_profile.character_id
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
        self._record_trace(
            event_type="turn_started",
            turn_id=turn_id,
            turn_input=turn_input,
            payload={
                "metadata": turn_input.metadata,
                "requested_character_id": turn_input.character_id,
                "active_character_id": active_character_id,
            },
            character_id=active_character_id,
        )
        try:
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
            character_context = self.character_provider.build_runtime_context(
                character_id=active_character_id,
                user_input=turn_input.message,
                metadata=turn_input.metadata,
            )
            user_message_id = self.message_store.save_message(
                session_id=turn_input.session_id,
                user_id=turn_input.user_id,
                role="user",
                content=turn_input.message,
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
            self._record_trace(
                event_type="prompt_context_built",
                turn_id=turn_id,
                turn_input=turn_input,
                payload={
                    "sections": [
                        {
                            "id": section.id,
                            "source_ids": section.source_ids,
                            "line_count": len(section.content_lines),
                        }
                        for section in prompt_context.sections
                    ],
                    "metadata": prompt_context.metadata,
                },
                character_id=active_character_id,
            )
            prompt_messages = self.prompt_compiler.compile_messages(context=prompt_context)
            response = self.response_generator.generate(
                messages=prompt_messages,
                user_input=turn_input.message,
                retrieved_memories=[m.content for m in retrieved_memories],
            )
            self._record_trace(
                event_type="response_generated",
                turn_id=turn_id,
                turn_input=turn_input,
                payload={"response_length": len(response)},
                character_id=active_character_id,
            )
            self.message_store.save_message(
                session_id=turn_input.session_id,
                user_id=turn_input.user_id,
                role="assistant",
                content=response,
            )

            state_update = self.state_updater.decide(
                user_input=turn_input.message,
                assistant_response=response,
            )
            self._record_trace(
                event_type="state_update_decided",
                turn_id=turn_id,
                turn_input=turn_input,
                payload={
                    "user_emotion": state_update.user_emotion,
                    "aemeath_mood": state_update.aemeath_mood,
                    "trust_delta": state_update.trust_delta,
                    "affection_delta": state_update.affection_delta,
                    "familiarity_delta": state_update.familiarity_delta,
                    "reason": state_update.reason,
                },
                character_id=active_character_id,
            )
            self.relationship_store.update_relationship_state(
                user_id=turn_input.user_id,
                character_id=active_character_id,
                mood=state_update.aemeath_mood,
                user_emotion=state_update.user_emotion,
                trust_delta=state_update.trust_delta,
                affection_delta=state_update.affection_delta,
                familiarity_delta=state_update.familiarity_delta,
                last_topic=state_update.last_topic,
            )

            memory_decision = self.memory_write_classifier.decide(
                user_input=turn_input.message,
                assistant_response=response,
            )
            created_memory_id = self.memory_writer.persist_decision(
                user_id=turn_input.user_id,
                decision=memory_decision,
                source_message_id=user_message_id,
                session_id=turn_input.session_id,
                character_id=active_character_id,
            )
            window_memory_job_id = self.memory_writer.enqueue_window_if_due(
                user_id=turn_input.user_id,
                session_id=turn_input.session_id,
                character_id=active_character_id,
            )

            state_delta = {
                "trust": state_update.trust_delta,
                "affection": state_update.affection_delta,
                "familiarity": state_update.familiarity_delta,
            }
            if memory_decision.should_write and not created_memory_id:
                warnings.append("memory_write_skipped_or_deduplicated")
            self._record_trace(
                event_type="memory_write_decided",
                turn_id=turn_id,
                turn_input=turn_input,
                payload={
                    "should_write": memory_decision.should_write,
                    "operation": memory_decision.operation,
                    "created_memory_id": created_memory_id,
                    "window_memory_job_id": window_memory_job_id,
                    "reason": memory_decision.reason,
                },
                character_id=active_character_id,
            )

            run.ended_at = utc_now()
            run.status = "completed"
            run.warnings = warnings
            self._record_trace(
                event_type="turn_completed",
                turn_id=turn_id,
                turn_input=turn_input,
                payload={
                    "status": run.status,
                    "started_at": run.started_at,
                    "ended_at": run.ended_at,
                    "warnings": warnings,
                },
                character_id=active_character_id,
            )
            self._record_prompt_run(
                turn_id=turn_id,
                turn_input=turn_input,
                active_character_id=active_character_id,
                prompt_messages=prompt_messages,
                prompt_context_metadata=prompt_context_metadata,
                sections=prompt_sections,
                response=response,
                warnings=warnings,
                status="completed",
            )

            return AgentTurnOutput(
                turn_id=turn_id,
                session_id=turn_input.session_id,
                response=response,
                used_memory_ids=[m.id for m in retrieved_memories],
                state_delta=state_delta,
                warnings=warnings,
                used_memories=[
                    UsedMemory(
                        id=result.record.id,
                        content=result.record.content,
                        score=result.score,
                        match_reason=result.match_reason,
                    )
                    for result in memory_results
                ],
            )
        except Exception as exc:
            error_info = map_exception(exc)
            log_error = error_info.to_log_dict()
            run.ended_at = utc_now()
            run.status = "failed"
            run.warnings = warnings
            self._record_trace(
                event_type="turn_failed",
                turn_id=turn_id,
                turn_input=turn_input,
                payload={
                    "status": run.status,
                    "started_at": run.started_at,
                    "ended_at": run.ended_at,
                    "warnings": warnings,
                    "error": log_error,
                },
                level="error",
                character_id=active_character_id,
            )
            if (
                prompt_messages is not None
                and prompt_context_metadata is not None
                and prompt_sections is not None
            ):
                self._record_prompt_run(
                    turn_id=turn_id,
                    turn_input=turn_input,
                    active_character_id=active_character_id,
                    prompt_messages=prompt_messages,
                    prompt_context_metadata=prompt_context_metadata,
                    sections=prompt_sections,
                    response=response,
                    warnings=warnings,
                    status="failed",
                    error_code=error_info.code.value,
                    error_type=error_info.error_type,
                    error_public_message=error_info.public_message,
                    error=log_error["internal_message"],
                )
            raise
