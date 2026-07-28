from __future__ import annotations

from typing import Any, Protocol

from app.agent.prompt_context import PromptContext
from app.character.schemas import CharacterProfile, CharacterRuntimeContext
from app.memory.schemas import MemoryQuery, MemorySearchResult
from app.retrieval.schemas import EvidencePack
from app.schemas import MemoryDecision, MemoryRecord, MessageRecord, RelationshipState, StateUpdate
from app.semantic.schemas import TurnSemanticContext


class CharacterProviderPort(Protocol):
    def load_profile(self, *, character_id: str | None = None) -> CharacterProfile:
        ...

    def build_runtime_context(
        self,
        *,
        character_id: str | None = None,
        user_input: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> CharacterRuntimeContext:
        ...


class MemoryReaderPort(Protocol):
    def retrieve_query(
        self,
        *,
        memory_query: MemoryQuery,
        semantic_context: TurnSemanticContext | None = None,
    ) -> list[MemorySearchResult]:
        ...

    def retrieve(self, *, user_id: str, query: str, limit: int = 8) -> list[MemoryRecord]:
        ...


class MemoryWriterPort(Protocol):
    def persist_decision(
        self,
        *,
        user_id: str,
        decision: MemoryDecision,
        source_message_id: str | None = None,
        session_id: str,
        character_id: str,
    ) -> str | None:
        ...

    def enqueue_window_if_due(
        self,
        *,
        user_id: str,
        session_id: str,
        character_id: str,
    ) -> str | None:
        ...


class MemoryWriteClassifierPort(Protocol):
    def decide(self, *, user_input: str, assistant_response: str) -> MemoryDecision:
        ...


class StateUpdaterPort(Protocol):
    def decide(self, *, user_input: str, assistant_response: str) -> StateUpdate:
        ...


class ContextBuilderPort(Protocol):
    def build_context(
        self,
        *,
        character_context: CharacterRuntimeContext,
        user_input: str,
        recent_messages: list[MessageRecord],
        long_term_memories: list[MemoryRecord],
        relationship_state: RelationshipState,
        evidence_pack: EvidencePack | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> PromptContext:
        ...

    def build_messages(
        self,
        *,
        character_context: CharacterRuntimeContext,
        user_input: str,
        recent_messages: list[MessageRecord],
        long_term_memories: list[MemoryRecord],
        relationship_state: RelationshipState,
        evidence_pack: EvidencePack | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> list[dict[str, str]]:
        ...


class ContextCompressorPort(Protocol):
    def compress(self, *, context: PromptContext) -> PromptContext:
        ...


class ResponseGeneratorPort(Protocol):
    def generate(
        self,
        *,
        messages: list[dict[str, str]],
        user_input: str,
        retrieved_memories: list[str],
    ) -> str:
        ...
