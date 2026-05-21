from __future__ import annotations

from app.agent.context_builder import ContextBuilder
from app.agent.dialogue_controller import DialogueController
from app.agent.response_generator import ResponseGenerator
from app.agent.response_mode import ResponseModeRouter
from app.agent.state_updater import StateUpdater
from app.character.provider import CharacterProvider
from app.config import Settings
from app.db.sqlite_store import SQLiteStore
from app.memory.admin import MemoryAdminService
from app.memory.classifier import MemoryWriteClassifier
from app.memory.reader import MemoryReader
from app.memory.writer import MemoryWriter
from app.model.deepseek import DeepSeekClient, FallbackChatModelClient
from app.model.mock import MockChatModelClient
from app.observability.noop import NoopTraceLogger
from app.retrieval.provider import EmptyEvidenceProvider
from app.state.classifier import StateUpdateClassifier


def build_controller(
    settings: Settings | None = None,
) -> tuple[DialogueController, MemoryAdminService, Settings, SQLiteStore]:
    settings = settings or Settings.from_env()
    settings.validate()
    store = SQLiteStore(settings.db_path)
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

    memory_reader = MemoryReader(store)
    memory_writer = MemoryWriter(store)
    response_generator = ResponseGenerator(chat_model)
    state_updater = StateUpdater(StateUpdateClassifier())
    controller = DialogueController(
        settings=settings,
        message_store=store,
        relationship_store=store,
        memory_reader=memory_reader,
        memory_writer=memory_writer,
        memory_write_classifier=MemoryWriteClassifier(),
        state_updater=state_updater,
        response_mode_router=ResponseModeRouter(),
        character_provider=CharacterProvider(settings.character_card_path),
        evidence_provider=EmptyEvidenceProvider(),
        context_builder=ContextBuilder(settings.prompt_template_path),
        response_generator=response_generator,
        trace_logger=NoopTraceLogger(),
    )
    memory_admin = MemoryAdminService(store)
    return controller, memory_admin, settings, store
