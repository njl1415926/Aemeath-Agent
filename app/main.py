from __future__ import annotations

from app.accounts.service import AccountService
from app.agent.context_builder import ContextBuilder
from app.agent.dialogue_controller import DialogueController
from app.agent.response_generator import ResponseGenerator
from app.agent.state_updater import StateUpdater
from app.auth.service import hash_dev_token
from app.character.provider import CharacterRegistry
from app.config import Settings
from app.db.sqlite_store import SQLiteStore
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
from app.memory.runtime import build_memory_embedding_provider
from app.memory.writer import QueuedMemoryWriter
from app.model.deepseek import DeepSeekClient, FallbackChatModelClient
from app.model.mock import MockChatModelClient
from app.observability.app_logger import JsonlAppLogger
from app.observability.noop import NoopAppLogger, NoopPromptRunLogger, NoopTraceLogger
from app.observability.prompt_run_logger import JsonlPromptRunLogger
from app.observability.trace_logger import JsonlTraceLogger
from app.retrieval.provider import WorldInfoEvidenceProvider
from app.services.agent_service import AgentService
from app.state.classifier import StateUpdateClassifier


def build_controller(
    settings: Settings | None = None,
) -> tuple[DialogueController, MemoryAdminService, Settings, SQLiteStore]:
    settings = settings or Settings.from_env()
    settings.validate()
    store = SQLiteStore(settings.db_path)
    _bootstrap_accounts(settings=settings, store=store)
    if settings.use_mock_llm:
        chat_model = MockChatModelClient()
    else:
        deepseek_client = DeepSeekClient(
            api_key=settings.deepseek_api_key or "",
            base_url=settings.deepseek_base_url,
            model=settings.aemeath_model,
            thinking=settings.deepseek_thinking,
            reasoning_effort=settings.deepseek_reasoning_effort,
        )
        chat_model = FallbackChatModelClient(
            primary=deepseek_client,
            fallback=MockChatModelClient(),
        )

    memory_extraction_service = None
    if settings.enable_long_term_memory:
        memory_embedding_provider = build_memory_embedding_provider(settings)
        extraction_model = (
            MockMemoryExtractionModelClient() if settings.use_mock_llm else chat_model
        )
        memory_extraction_service = MemoryExtractionService(
            store=store,
            model_client=extraction_model,
            embedding_provider=memory_embedding_provider,
        )
        memory_reader = MemoryReader(
            store,
            embedding_provider=memory_embedding_provider,
            recall_top_k=settings.memory_recall_top_k,
            recall_threshold=settings.memory_recall_threshold,
        )
        memory_writer = QueuedMemoryWriter(
            store,
            window_size=settings.memory_window_size,
            enable_window_extraction=settings.enable_memory_window_extraction,
        )
        memory_write_classifier = MemoryWriteClassifier()
    else:
        memory_reader = DisabledMemoryReader()
        memory_writer = DisabledMemoryWriter()
        memory_write_classifier = DisabledMemoryWriteClassifier()
    response_generator = ResponseGenerator(chat_model)
    state_updater = StateUpdater(StateUpdateClassifier())
    if settings.enable_prompt_run_log:
        prompt_run_logger = JsonlPromptRunLogger(root_dir=settings.prompt_run_log_root)
    else:
        prompt_run_logger = NoopPromptRunLogger()
    if settings.enable_trace_log:
        trace_logger = JsonlTraceLogger(root_dir=settings.trace_log_root)
    else:
        trace_logger = NoopTraceLogger()
    controller = DialogueController(
        settings=settings,
        session_store=store,
        message_store=store,
        relationship_store=store,
        memory_reader=memory_reader,
        memory_writer=memory_writer,
        memory_write_classifier=memory_write_classifier,
        state_updater=state_updater,
        character_provider=CharacterRegistry.from_single_card(
            character_card_path=settings.character_card_path,
            default_character_id=settings.default_character_id,
        ),
        evidence_provider=WorldInfoEvidenceProvider(),
        context_builder=ContextBuilder(settings.prompt_template_path),
        response_generator=response_generator,
        trace_logger=trace_logger,
        prompt_run_logger=prompt_run_logger,
    )
    memory_admin = MemoryAdminService(
        store,
        session_store=store,
        extraction_service=memory_extraction_service,
    )
    return controller, memory_admin, settings, store


def _bootstrap_accounts(*, settings: Settings, store: SQLiteStore) -> None:
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


def build_agent_service(
    settings: Settings | None = None,
) -> tuple[AgentService, MemoryAdminService, Settings, SQLiteStore]:
    controller, memory_admin, resolved_settings, store = build_controller(settings=settings)
    if resolved_settings.enable_app_log:
        app_logger = JsonlAppLogger(root_dir=resolved_settings.app_log_root)
    else:
        app_logger = NoopAppLogger()
    service = AgentService(controller=controller, app_logger=app_logger)
    return service, memory_admin, resolved_settings, store
