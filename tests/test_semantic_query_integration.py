from __future__ import annotations

from dataclasses import replace

from app.embeddings.config import load_embedding_space_profile
from app.embeddings.errors import EmbeddingUnavailableError
from app.embeddings.observability import ObservableEmbeddingProvider
from app.embeddings.providers.hash import DeterministicHashEmbeddingProvider
from app.embeddings.synchronized import SynchronizedEmbeddingProvider
from app.main import build_controller
from app.memory.reader import MemoryReader
from app.memory.schemas import MemoryQuery
from app.retrieval.provider import WorldInfoEvidenceProvider
from app.retrieval.schemas import EvidencePack
from app.schemas import AgentTurnInput
from app.semantic.coordinator import SemanticQueryCoordinator
from app.semantic.disabled import DisabledSemanticQueryCoordinator
from app.semantic.schemas import SemanticTurnInput


def test_main_runtime_injects_one_synchronized_wrapper_by_identity(settings) -> None:
    configured = replace(settings, enable_long_term_memory=True)

    controller, memory_admin, _resolved_settings, _store = build_controller(
        settings=configured
    )

    coordinator_provider = controller.semantic_query_coordinator._provider
    assert isinstance(coordinator_provider, SynchronizedEmbeddingProvider)
    assert isinstance(coordinator_provider._provider, ObservableEmbeddingProvider)
    assert isinstance(controller.memory_reader, MemoryReader)
    assert controller.memory_reader.query_embedding_source is not None
    assert controller.memory_reader.query_embedding_source._provider is coordinator_provider
    assert memory_admin.extraction_service is not None
    assert memory_admin.extraction_service.embedding_provider is coordinator_provider
    assert isinstance(controller.evidence_provider, WorldInfoEvidenceProvider)


def test_controller_filters_semantic_roles_and_shares_context_identity(settings) -> None:
    controller, _memory_admin, _resolved_settings, store = build_controller(settings=settings)
    store.ensure_session(
        session_id="session_semantic",
        user_id="user_default",
        character_id="aemeath",
    )
    system_id = store.save_message(
        session_id="session_semantic",
        user_id="user_default",
        role="system",
        content="system body",
    )
    tool_id = store.save_message(
        session_id="session_semantic",
        user_id="user_default",
        role="tool",
        content="tool body",
    )
    prior_user_id = store.save_message(
        session_id="session_semantic",
        user_id="user_default",
        role="user",
        content="继续",
    )
    prior_assistant_id = store.save_message(
        session_id="session_semantic",
        user_id="user_default",
        role="assistant",
        content="上一轮回答",
    )

    class _CapturingCoordinator:
        def __init__(self) -> None:
            self.turn_input = None
            self.context = None

        def prepare_turn_context(self, *, turn_input):
            self.turn_input = turn_input
            self.context = DisabledSemanticQueryCoordinator().prepare_turn_context(
                turn_input=turn_input
            )
            return self.context

    class _CapturingMemoryReader:
        def __init__(self) -> None:
            self.semantic_context = None

        def retrieve_query(
            self,
            *,
            memory_query: MemoryQuery,
            semantic_context=None,
        ):
            self.semantic_context = semantic_context
            return []

        def retrieve(self, *, user_id: str, query: str, limit: int = 8):
            return []

    class _CapturingEvidenceProvider:
        def __init__(self) -> None:
            self.semantic_context = None

        def build_evidence_pack(self, **kwargs):
            self.semantic_context = kwargs["semantic_context"]
            return EvidencePack()

    coordinator = _CapturingCoordinator()
    memory_reader = _CapturingMemoryReader()
    evidence_provider = _CapturingEvidenceProvider()
    controller.semantic_query_coordinator = coordinator
    controller.memory_reader = memory_reader
    controller.evidence_provider = evidence_provider

    output = controller.handle_user_message(
        AgentTurnInput(
            user_id="user_default",
            session_id="session_semantic",
            character_id="aemeath",
            message="继续",
        )
    )

    assert output.response
    assert coordinator.turn_input is not None
    semantic_ids = tuple(
        message.id for message in coordinator.turn_input.recent_messages
    )
    assert system_id not in semantic_ids
    assert tool_id not in semantic_ids
    assert semantic_ids == (prior_user_id, prior_assistant_id)
    assert all(
        message.role in {"user", "assistant"}
        for message in coordinator.turn_input.recent_messages
    )
    assert coordinator.turn_input.current_user_message_id not in semantic_ids
    assert memory_reader.semantic_context is coordinator.context
    assert evidence_provider.semantic_context is coordinator.context


def test_world_info_ignores_disabled_ready_and_failed_semantic_contexts(settings) -> None:
    controller, _memory_admin, _resolved_settings, _store = build_controller(settings=settings)
    provider = controller.evidence_provider
    assert isinstance(provider, WorldInfoEvidenceProvider)
    query = "爱弥斯是谁"
    turn_input = SemanticTurnInput(
        turn_id="turn_world",
        user_id="user_default",
        session_id="session_world",
        character_id="aemeath",
        current_user_message_id="message_world",
        current_input=query,
        recent_messages=(),
    )
    hash_provider = DeterministicHashEmbeddingProvider(
        profile=load_embedding_space_profile("deterministic_hash_v1")
    )

    class _UnavailableProvider:
        signature = hash_provider.signature

        def embed(self, request):
            raise EmbeddingUnavailableError(reason="test_unavailable")

    semantic_contexts = (
        DisabledSemanticQueryCoordinator().prepare_turn_context(turn_input=turn_input),
        SemanticQueryCoordinator(
            provider=hash_provider,
            active_roles=("canonical_lore",),
        ).prepare_turn_context(turn_input=turn_input),
        SemanticQueryCoordinator(
            provider=_UnavailableProvider(),
            active_roles=("canonical_lore",),
        ).prepare_turn_context(turn_input=turn_input),
    )

    without_context = provider.build_evidence_pack(
        user_id="user_default",
        character_id="aemeath",
        query=query,
    )
    for semantic_context in semantic_contexts:
        with_context = provider.build_evidence_pack(
            user_id="user_default",
            character_id="aemeath",
            query=query,
            semantic_context=semantic_context,
        )
        assert with_context == without_context
