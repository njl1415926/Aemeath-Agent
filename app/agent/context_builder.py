from __future__ import annotations

from pathlib import Path

from app.agent.prompt_context import PromptCompiler, PromptContext, PromptSection
from app.character.schemas import CharacterRuntimeContext
from app.retrieval.schemas import EvidencePack
from app.schemas import MemoryRecord, MessageRecord, RelationshipState


class ContextBuilder:
    def __init__(self, prompt_template_path: Path):
        self.prompt_template_path = prompt_template_path
        self.prompt_template = prompt_template_path.read_text(encoding="utf-8")
        self.prompt_compiler = PromptCompiler()

    def build_context(
        self,
        *,
        character_context: CharacterRuntimeContext,
        response_mode: str,
        user_input: str,
        recent_messages: list[MessageRecord],
        long_term_memories: list[MemoryRecord],
        relationship_state: RelationshipState,
        evidence_pack: EvidencePack | None = None,
    ) -> PromptContext:
        evidence_pack = evidence_pack or EvidencePack()
        memory_lines = [
            f"- [{m.id}] ({m.memory_type}, importance={m.importance}): {m.content}"
            for m in long_term_memories
        ]
        if not memory_lines:
            memory_lines = ["- (none)"]

        recent_lines = [f"{m.role}: {m.content}" for m in recent_messages]
        if not recent_lines:
            recent_lines = ["(no recent messages)"]

        lore_lines = [f"- [{item.id}] {item.content}" for item in evidence_pack.lore_results]
        if not lore_lines:
            lore_lines = ["- (none)"]

        voice_lines = [f"- [{item.id}] {item.content}" for item in evidence_pack.voice_results]
        if not voice_lines:
            voice_lines = ["- (none)"]

        sections = [
            PromptSection(
                id="base_instructions",
                title="Base instructions:",
                content_lines=[
                    self.prompt_template.strip(),
                    "",
                    f"Response mode: {response_mode}",
                ],
            ),
            PromptSection(
                id="character_context",
                title="Character context:",
                source_ids=character_context.source_doc_ids,
                content_lines=[
                    f"Character: {character_context.display_name} "
                    f"({character_context.english_name})",
                    f"Character id: {character_context.character_id}",
                    f"Character version: {character_context.version}",
                    f"Canon point: {character_context.canon_point}",
                    f"Identity: {character_context.identity}",
                    f"Relation to user: {character_context.relation_to_user}",
                ],
            ),
            PromptSection(
                id="persona_rules",
                title="Persona rules:",
                content_lines=[f"- {rule}" for rule in character_context.persona_rules],
            ),
            PromptSection(
                id="speech_rules",
                title="Speech rules:",
                content_lines=[f"- {rule}" for rule in character_context.speech_rules],
            ),
            PromptSection(
                id="boundaries",
                title="Boundaries:",
                content_lines=[f"- {rule}" for rule in character_context.boundaries],
            ),
            PromptSection(
                id="avoid",
                title="Avoid:",
                content_lines=[f"- {rule}" for rule in character_context.avoid],
            ),
            PromptSection(
                id="relationship_state",
                title="Current relationship state:",
                content_lines=[
                    f"- mood: {relationship_state.mood}",
                    f"- user_emotion: {relationship_state.user_emotion}",
                    f"- trust: {relationship_state.trust}",
                    f"- affection: {relationship_state.affection}",
                    f"- familiarity: {relationship_state.familiarity}",
                    f"- last_topic: {relationship_state.last_topic}",
                ],
            ),
            PromptSection(
                id="interaction_memories",
                title="Long-term user memories:",
                source_ids=[m.id for m in long_term_memories],
                content_lines=memory_lines,
            ),
            PromptSection(
                id="canon_evidence",
                title="Canon evidence:",
                source_ids=[item.id for item in evidence_pack.lore_results],
                content_lines=lore_lines,
            ),
            PromptSection(
                id="voice_evidence",
                title="Voice evidence:",
                source_ids=[item.id for item in evidence_pack.voice_results],
                content_lines=voice_lines,
            ),
            PromptSection(
                id="recent_conversation",
                title="Recent conversation:",
                source_ids=[m.id for m in recent_messages],
                content_lines=recent_lines,
            ),
        ]

        return PromptContext(
            response_mode=response_mode,
            user_input=user_input,
            sections=sections,
            metadata={
                "character_id": character_context.character_id,
                "evidence_ids": {
                    "lore": [item.id for item in evidence_pack.lore_results],
                    "voice": [item.id for item in evidence_pack.voice_results],
                },
                "memory_ids": [m.id for m in long_term_memories],
            },
        )

    def build_messages(
        self,
        *,
        character_context: CharacterRuntimeContext,
        response_mode: str,
        user_input: str,
        recent_messages: list[MessageRecord],
        long_term_memories: list[MemoryRecord],
        relationship_state: RelationshipState,
        evidence_pack: EvidencePack | None = None,
    ) -> list[dict[str, str]]:
        prompt_context = self.build_context(
            character_context=character_context,
            response_mode=response_mode,
            user_input=user_input,
            recent_messages=recent_messages,
            long_term_memories=long_term_memories,
            relationship_state=relationship_state,
            evidence_pack=evidence_pack,
        )
        return self.prompt_compiler.compile_messages(context=prompt_context)
