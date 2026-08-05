from __future__ import annotations

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
from app.agent.turn_engine import (
    DialogueTurnEngine,
    PreparedDialogueTurn,
    TurnCompletionPlan,
)
from app.config import Settings
from app.observability.ports import PromptRunLoggerPort, TraceLoggerPort
from app.retrieval.ports import EvidenceProviderPort
from app.schemas import AgentTurnInput, AgentTurnOutput
from app.semantic.ports import SemanticQueryCoordinatorPort
from app.session.ports import SessionStorePort
from app.state.store import RelationshipStore
from app.utils.ids import new_id


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
    ) -> None:
        self.turn_engine = DialogueTurnEngine(
            settings=settings,
            session_store=session_store,
            message_store=message_store,
            relationship_store=relationship_store,
            memory_reader=memory_reader,
            memory_writer=memory_writer,
            memory_write_classifier=memory_write_classifier,
            state_updater=state_updater,
            character_provider=character_provider,
            evidence_provider=evidence_provider,
            context_builder=context_builder,
            trace_logger=trace_logger,
            prompt_run_logger=prompt_run_logger,
            semantic_query_coordinator=semantic_query_coordinator,
        )
        self.response_generator = response_generator

    @property
    def settings(self) -> Settings:
        return self.turn_engine.settings

    @settings.setter
    def settings(self, value: Settings) -> None:
        self.turn_engine.settings = value

    @property
    def session_store(self) -> SessionStorePort:
        return self.turn_engine.session_store

    @session_store.setter
    def session_store(self, value: SessionStorePort) -> None:
        self.turn_engine.session_store = value

    @property
    def message_store(self) -> MessageStore:
        return self.turn_engine.message_store

    @message_store.setter
    def message_store(self, value: MessageStore) -> None:
        self.turn_engine.message_store = value

    @property
    def relationship_store(self) -> RelationshipStore:
        return self.turn_engine.relationship_store

    @relationship_store.setter
    def relationship_store(self, value: RelationshipStore) -> None:
        self.turn_engine.relationship_store = value

    @property
    def semantic_query_coordinator(self) -> SemanticQueryCoordinatorPort:
        return self.turn_engine.semantic_query_coordinator

    @semantic_query_coordinator.setter
    def semantic_query_coordinator(
        self,
        value: SemanticQueryCoordinatorPort,
    ) -> None:
        self.turn_engine.semantic_query_coordinator = value

    @property
    def memory_reader(self) -> MemoryReaderPort:
        return self.turn_engine.memory_reader

    @memory_reader.setter
    def memory_reader(self, value: MemoryReaderPort) -> None:
        self.turn_engine.memory_reader = value

    @property
    def memory_writer(self) -> MemoryWriterPort:
        return self.turn_engine.memory_writer

    @memory_writer.setter
    def memory_writer(self, value: MemoryWriterPort) -> None:
        self.turn_engine.memory_writer = value

    @property
    def memory_write_classifier(self) -> MemoryWriteClassifierPort:
        return self.turn_engine.memory_write_classifier

    @memory_write_classifier.setter
    def memory_write_classifier(self, value: MemoryWriteClassifierPort) -> None:
        self.turn_engine.memory_write_classifier = value

    @property
    def state_updater(self) -> StateUpdaterPort:
        return self.turn_engine.state_updater

    @state_updater.setter
    def state_updater(self, value: StateUpdaterPort) -> None:
        self.turn_engine.state_updater = value

    @property
    def character_provider(self) -> CharacterProviderPort:
        return self.turn_engine.character_provider

    @character_provider.setter
    def character_provider(self, value: CharacterProviderPort) -> None:
        self.turn_engine.character_provider = value

    @property
    def evidence_provider(self) -> EvidenceProviderPort:
        return self.turn_engine.evidence_provider

    @evidence_provider.setter
    def evidence_provider(self, value: EvidenceProviderPort) -> None:
        self.turn_engine.evidence_provider = value

    @property
    def context_builder(self) -> ContextBuilderPort:
        return self.turn_engine.context_builder

    @context_builder.setter
    def context_builder(self, value: ContextBuilderPort) -> None:
        self.turn_engine.context_builder = value

    @property
    def trace_logger(self) -> TraceLoggerPort:
        return self.turn_engine.trace_logger

    @trace_logger.setter
    def trace_logger(self, value: TraceLoggerPort) -> None:
        self.turn_engine.trace_logger = value

    @property
    def prompt_run_logger(self) -> PromptRunLoggerPort:
        return self.turn_engine.prompt_run_logger

    @prompt_run_logger.setter
    def prompt_run_logger(self, value: PromptRunLoggerPort) -> None:
        self.turn_engine.prompt_run_logger = value

    @property
    def prompt_compiler(self) -> PromptCompiler:
        return self.turn_engine.prompt_compiler

    @prompt_compiler.setter
    def prompt_compiler(self, value: PromptCompiler) -> None:
        self.turn_engine.prompt_compiler = value

    def handle_user_message(self, turn_input: AgentTurnInput) -> AgentTurnOutput:
        preflight = self.turn_engine.preflight(
            turn_input=turn_input,
            turn_id=new_id("turn"),
        )
        prepared: PreparedDialogueTurn | None = None
        response: str | None = None
        try:
            character_context = self.turn_engine.prepare_character_context(
                preflight=preflight
            )
            user_message_id = self.message_store.save_message(
                session_id=turn_input.session_id,
                user_id=turn_input.user_id,
                role="user",
                content=turn_input.message,
            )
            prepared = self.turn_engine.prepare(
                preflight=preflight,
                user_message_id=user_message_id,
                character_context=character_context,
            )
            response = self.response_generator.generate(
                messages=prepared.prompt_messages,
                user_input=turn_input.message,
                retrieved_memories=[
                    result.record.content for result in prepared.memory_results
                ],
            )
            self.turn_engine.record_response_generated(
                preflight=preflight,
                response=response,
            )
            return self._complete_synchronously(
                prepared=prepared,
                response=response,
            )
        except Exception as exc:
            self.turn_engine.record_failed(
                preflight=preflight,
                prepared=prepared,
                response=response,
                error=exc,
            )
            raise

    def _complete_synchronously(
        self,
        *,
        prepared: PreparedDialogueTurn,
        response: str,
    ) -> AgentTurnOutput:
        preflight = prepared.preflight
        turn_input = preflight.turn_input
        active_character_id = preflight.active_character_id
        self.message_store.save_message(
            session_id=turn_input.session_id,
            user_id=turn_input.user_id,
            role="assistant",
            content=response,
        )
        state_update = self.turn_engine.decide_state_update(
            prepared=prepared,
            assistant_response=response,
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
        memory_decision = self.turn_engine.decide_memory_write(
            prepared=prepared,
            assistant_response=response,
        )
        created_memory_id = self.memory_writer.persist_decision(
            user_id=turn_input.user_id,
            decision=memory_decision,
            source_message_id=prepared.user_message_id,
            session_id=turn_input.session_id,
            character_id=active_character_id,
        )
        window_memory_job_id = self.memory_writer.enqueue_window_if_due(
            user_id=turn_input.user_id,
            session_id=turn_input.session_id,
            character_id=active_character_id,
        )
        if memory_decision.should_write and not created_memory_id:
            preflight.warnings.append("memory_write_skipped_or_deduplicated")
        self.turn_engine.record_memory_write_decided(
            prepared=prepared,
            memory_decision=memory_decision,
            created_memory_id=created_memory_id,
            window_memory_job_id=window_memory_job_id,
        )
        completion_plan: TurnCompletionPlan = self.turn_engine.build_completion_plan(
            state_update=state_update,
            memory_decision=memory_decision,
        )
        self.turn_engine.record_completed(
            prepared=prepared,
            response=response,
        )
        return self.turn_engine.build_output(
            prepared=prepared,
            response=response,
            completion_plan=completion_plan,
        )
