from __future__ import annotations

from typing import Literal

from app.accounts.service import AccountService
from app.agent.context_budget import ContextBudgetPolicy, DeterministicContextCompressor
from app.agent.context_builder import ContextBuilder
from app.agent.dialogue_controller import DialogueController
from app.agent.response_generator import ResponseGenerator
from app.agent.state_updater import StateUpdater
from app.auth.service import hash_dev_token
from app.character.provider import CharacterRegistry
from app.config import Settings
from app.db.store_factory import StoreFacade, create_store
from app.embeddings.synchronized import SynchronizedEmbeddingProvider
from app.errors import AemeathError, ErrorCode
from app.memory.admin import MemoryAdminService
from app.memory.classifier import MemoryWriteClassifier
from app.memory.disabled import (
    DisabledMemoryReader,
    DisabledMemoryWriteClassifier,
    DisabledMemoryWriter,
)
from app.memory.extractor import MemoryExtractionService
from app.memory.mock_extractor import MockMemoryExtractionModelClient
from app.memory.reader import MemoryReader
from app.memory.runtime import build_memory_embedding_runtime
from app.memory.writer import QueuedMemoryWriter
from app.model.deepseek import DeepSeekClient, FallbackChatModelClient
from app.model.mock import MockChatModelClient
from app.observability.noop import NoopPromptRunLogger, NoopTraceLogger
from app.observability.ports import AppLoggerPort
from app.observability.prompt_run_logger import JsonlPromptRunLogger
from app.observability.runtime import create_app_logger
from app.observability.safe import SafeTraceLogger
from app.observability.trace_logger import JsonlTraceLogger
from app.retrieval.provider import WorldInfoEvidenceProvider
from app.runtime.guardrails.admission import InProcessDialogueAdmission
from app.runtime.guardrails.concurrency import (
    ConcurrencyGate,
    LimitedChatModelClient,
    LimitedEmbeddingProvider,
)
from app.runtime.guardrails.input_limits import ChatInputLimitPolicyV01
from app.runtime.resources import ApplicationRuntime, RuntimeResources
from app.semantic.coordinator import SemanticQueryCoordinator
from app.semantic.disabled import DisabledSemanticQueryCoordinator
from app.semantic.eligibility import DeterministicSemanticEligibilityPolicyV01
from app.semantic.query_builder import NeutralTurnQueryBuilderV01
from app.services.agent_service import AgentService
from app.state.classifier import StateUpdateClassifier


def build_application_runtime(
    settings: Settings | None = None,
    *,
    surface: Literal["cli", "api"],
) -> ApplicationRuntime:
    if surface not in {"cli", "api"}:
        raise _config_error("runtime_surface_invalid")
    resolved = settings or Settings.from_env()
    resolved.validate()
    if surface == "api" and resolved.api_enable_model_fallback:
        raise _config_error("api_model_fallback_not_supported")

    resources = RuntimeResources()
    app_logger = build_app_logger(resolved)
    try:
        store = create_store(db_path=resolved.db_path, backend=resolved.db_backend)
        _bootstrap_accounts(settings=resolved, store=store)
        llm_gate = ConcurrencyGate(
            limit=resolved.runtime_llm_max_concurrency,
            wait_timeout_seconds=resolved.runtime_llm_wait_timeout_seconds,
            reason="llm_concurrency",
        )
        dialogue_model, extraction_model = _build_models(
            settings=resolved,
            surface=surface,
            gate=llm_gate,
            resources=resources,
            app_logger=app_logger,
        )

        memory_extraction_service = None
        if resolved.enable_long_term_memory:
            embedding_runtime = build_memory_embedding_runtime(
                resolved,
                app_logger=app_logger,
            )
            resources.register(
                name="embedding_provider",
                close=embedding_runtime.close,
            )
            embedding_gate = ConcurrencyGate(
                limit=resolved.runtime_embedding_max_concurrency,
                wait_timeout_seconds=resolved.runtime_embedding_wait_timeout_seconds,
                reason="embedding_concurrency",
            )
            embedding_provider = LimitedEmbeddingProvider(
                gate=embedding_gate,
                delegate=SynchronizedEmbeddingProvider(embedding_runtime.provider),
                app_logger=app_logger,
            )
            memory_extraction_service = MemoryExtractionService(
                store=store,
                model_client=extraction_model,
                embedding_provider=embedding_provider,
                app_logger=app_logger,
            )
            memory_reader = MemoryReader(
                store,
                embedding_provider=embedding_provider,
                recall_top_k=resolved.memory_recall_top_k,
                recall_threshold=resolved.memory_recall_threshold,
                app_logger=app_logger,
            )
            memory_writer = QueuedMemoryWriter(
                store,
                window_size=resolved.memory_window_size,
                enable_window_extraction=resolved.enable_memory_window_extraction,
            )
            memory_write_classifier = MemoryWriteClassifier()
            semantic_query_coordinator = SemanticQueryCoordinator(
                provider=embedding_provider,
                active_roles=("memory_recall",),
                query_builder=NeutralTurnQueryBuilderV01(
                    eligibility_policy=DeterministicSemanticEligibilityPolicyV01(
                        enabled=resolved.enable_semantic_low_information_skip
                    )
                ),
                app_logger=app_logger,
                state_wait_timeout_seconds=resolved.runtime_embedding_wait_timeout_seconds,
            )
        else:
            memory_reader = DisabledMemoryReader()
            memory_writer = DisabledMemoryWriter()
            memory_write_classifier = DisabledMemoryWriteClassifier()
            semantic_query_coordinator = DisabledSemanticQueryCoordinator()

        context_compressor = DeterministicContextCompressor(
            policy=ContextBudgetPolicy(
                enabled=resolved.enable_context_budget,
                max_chars=resolved.context_budget_max_chars,
                recent_message_limit=resolved.context_budget_recent_message_limit,
                memory_limit=resolved.context_budget_memory_limit,
                evidence_limit=resolved.context_budget_evidence_limit,
            )
        )
        prompt_logger = (
            JsonlPromptRunLogger(root_dir=resolved.prompt_run_log_root)
            if resolved.enable_prompt_run_log
            else NoopPromptRunLogger()
        )
        trace_logger = SafeTraceLogger(
            JsonlTraceLogger(root_dir=resolved.trace_log_root)
            if resolved.enable_trace_log
            else NoopTraceLogger()
        )
        controller = DialogueController(
            settings=resolved,
            session_store=store,
            message_store=store,
            relationship_store=store,
            memory_reader=memory_reader,
            memory_writer=memory_writer,
            memory_write_classifier=memory_write_classifier,
            state_updater=StateUpdater(StateUpdateClassifier()),
            character_provider=CharacterRegistry.from_single_card(
                character_card_path=resolved.character_card_path,
                default_character_id=resolved.default_character_id,
            ),
            evidence_provider=WorldInfoEvidenceProvider(),
            context_builder=ContextBuilder(
                resolved.prompt_template_path,
                context_compressor=context_compressor,
            ),
            response_generator=ResponseGenerator(dialogue_model),
            trace_logger=trace_logger,
            prompt_run_logger=prompt_logger,
            semantic_query_coordinator=semantic_query_coordinator,
        )
        memory_admin = MemoryAdminService(
            store,
            session_store=store,
            extraction_service=memory_extraction_service,
        )
        service = AgentService(
            controller=controller,
            app_logger=app_logger,
            admission=InProcessDialogueAdmission(
                session_wait_timeout_seconds=resolved.runtime_session_wait_timeout_seconds,
                dialogue_limit=resolved.runtime_dialogue_max_concurrency,
                dialogue_wait_timeout_seconds=resolved.runtime_dialogue_wait_timeout_seconds,
            ),
            input_limit_policy=ChatInputLimitPolicyV01(
                message_max_chars=resolved.chat_message_max_chars,
                message_max_utf8_bytes=resolved.chat_message_max_utf8_bytes,
                metadata_max_utf8_bytes=resolved.chat_metadata_max_utf8_bytes,
            ),
        )
        return ApplicationRuntime(
            surface=surface,
            agent_service=service,
            memory_admin_service=memory_admin,
            store=store,
            settings=resolved,
            resources=resources,
            app_logger=app_logger,
        )
    except BaseException:
        resources.close()
        raise


def _build_models(
    *,
    settings: Settings,
    surface: Literal["cli", "api"],
    gate: ConcurrencyGate,
    resources: RuntimeResources,
    app_logger: AppLoggerPort,
):
    if settings.use_mock_llm:
        return (
            LimitedChatModelClient(
                gate=gate,
                delegate=MockChatModelClient(),
                purpose="dialogue",
                app_logger=app_logger,
            ),
            LimitedChatModelClient(
                gate=gate,
                delegate=MockMemoryExtractionModelClient(),
                purpose="memory_extraction",
                app_logger=app_logger,
            ),
        )
    deepseek = DeepSeekClient(
        api_key=settings.deepseek_api_key or "",
        base_url=settings.deepseek_base_url,
        model=settings.aemeath_model,
        thinking=settings.deepseek_thinking,
        reasoning_effort=settings.deepseek_reasoning_effort,
        connect_timeout_seconds=settings.deepseek_connect_timeout_seconds,
        read_timeout_seconds=settings.deepseek_read_timeout_seconds,
        write_timeout_seconds=settings.deepseek_write_timeout_seconds,
        pool_timeout_seconds=settings.deepseek_pool_timeout_seconds,
        max_retries=settings.deepseek_max_retries,
        retry_backoff_seconds=settings.deepseek_retry_backoff_seconds,
        retry_backoff_max_seconds=settings.deepseek_retry_backoff_max_seconds,
        retry_after_max_seconds=settings.deepseek_retry_after_max_seconds,
        max_output_tokens=settings.deepseek_max_output_tokens,
        max_connections=settings.runtime_llm_max_concurrency,
        app_logger=app_logger,
    )
    resources.register(name="deepseek_client", close=deepseek.close)
    dialogue_delegate = deepseek
    if surface == "cli" and settings.enable_model_fallback:
        dialogue_delegate = FallbackChatModelClient(
            primary=deepseek,
            fallback=MockChatModelClient(),
        )
    return (
        LimitedChatModelClient(
            gate=gate,
            delegate=dialogue_delegate,
            purpose="dialogue",
            app_logger=app_logger,
        ),
        LimitedChatModelClient(
            gate=gate,
            delegate=deepseek,
            purpose="memory_extraction",
            app_logger=app_logger,
        ),
    )


def _bootstrap_accounts(*, settings: Settings, store: StoreFacade) -> None:
    account_service = AccountService(store)
    account_service.ensure_account(
        account_id=settings.default_user_id,
        username=settings.default_user_id,
        display_name="Default User" if settings.default_user_id == "user_default" else None,
    )
    if settings.dev_auth_token:
        account_service.ensure_account(account_id=settings.dev_auth_user_id)
        account_service.set_dev_token_hash(
            account_id=settings.dev_auth_user_id,
            dev_token_hash=hash_dev_token(settings.dev_auth_token),
        )
    else:
        account_service.set_dev_token_hash(
            account_id=settings.dev_auth_user_id,
            dev_token_hash=None,
        )


def build_app_logger(settings: Settings) -> AppLoggerPort:
    return create_app_logger(
        enabled=settings.enable_app_log,
        root_dir=settings.app_log_root,
    )


def build_agent_service(settings: Settings | None = None) -> ApplicationRuntime:
    """Compatibility name returning the explicit lifecycle bundle, not a service tuple."""
    return build_application_runtime(settings=settings, surface="cli")


def build_controller(settings: Settings | None = None) -> ApplicationRuntime:
    """Compatibility name returning the explicit lifecycle bundle, not a controller tuple."""
    return build_application_runtime(settings=settings, surface="cli")


def _config_error(reason: str) -> AemeathError:
    return AemeathError(
        code=ErrorCode.CONFIG_INVALID,
        public_message="Configuration value is invalid.",
        internal_message=reason,
        metadata={"reason": reason},
    )
