from __future__ import annotations

from pathlib import Path
from typing import Any

from app.character.loader import (
    DEFAULT_CHARACTER_FALLBACK_PATH,
    CharacterDocument,
    load_character_document,
)
from app.character.schemas import CharacterProfile, CharacterRuntimeContext


def _build_legacy_profile(card: dict[str, Any]) -> CharacterProfile:
    roleplay_pack = card.get("roleplay_pack_path")
    return CharacterProfile(
        character_id=card.get("character_id", card["english_name"].lower()),
        display_name=card["name"],
        english_name=card["english_name"],
        source=card["source"],
        version=card["version"],
        canon_point=card.get("canon_point", "unknown"),
        roleplay_pack_path=Path(roleplay_pack) if roleplay_pack else None,
    )


def _component(document: CharacterDocument, name: str, top_key: str) -> Any:
    component = document.data["components"][name]
    if top_key not in component:
        raise ValueError(f"Character component {name} missing top-level key {top_key}.")
    return component[top_key]


def _build_manifest_profile(document: CharacterDocument) -> CharacterProfile:
    manifest = document.data["manifest"]
    profile = _component(document, "profile", "profile")
    roleplay_pack = profile.get("roleplay_pack_path")
    return CharacterProfile(
        character_id=manifest["character_id"],
        display_name=profile["display_name"],
        english_name=profile["english_name"],
        source=profile["source"],
        version=manifest["version"],
        canon_point=profile.get("canon_point", "unknown"),
        roleplay_pack_path=Path(roleplay_pack) if roleplay_pack else None,
    )


def _should_inject(
    item: dict[str, Any],
    *,
    context_tags: set[str],
    default_when_missing: bool,
) -> bool:
    inject = item.get("inject")
    if not isinstance(inject, dict):
        return default_when_missing
    tags = set(inject.get("context_tags") or [])
    return (
        bool(inject.get("default", default_when_missing))
        or bool(context_tags & tags)
    )


def _selected_contents(
    items: list[dict[str, Any]],
    *,
    context_tags: set[str],
    default_when_missing: bool = True,
) -> list[str]:
    return [
        item["content"]
        for item in items
        if "content" in item
        and _should_inject(
            item,
            context_tags=context_tags,
            default_when_missing=default_when_missing,
        )
    ]


def _format_style_example(item: dict[str, Any]) -> str:
    lines = [f"User context: {item['user_context']}"]
    if sample_user_input := item.get("sample_user_input"):
        lines.append(f"Sample user input: {sample_user_input}")
    if response_intent := item.get("response_intent"):
        lines.append(f"Intent: {response_intent}")
    if response := item.get("response"):
        lines.append(f"Aemeath response: {response}")
    elif responses := item.get("responses"):
        lines.append("Aemeath response variants:")
        lines.extend(f"- {response}" for response in responses)
    else:
        raise ValueError(f"Style example {item.get('id', '<unknown>')} missing response(s).")
    return "\n".join(lines)


def _selected_style_examples(
    items: list[dict[str, Any]],
) -> list[str]:
    return [_format_style_example(item) for item in items]


def _join_parts(parts: list[str]) -> str:
    return "\n".join(part for part in parts if part)


def _infer_context_tags(
    *,
    user_input: str,
    metadata: dict[str, Any],
) -> set[str]:
    tags = set(metadata.get("character_context_tags") or [])
    lore_hints = {
        "\u5267\u60c5",
        "\u539f\u4f5c",
        "\u8bbe\u5b9a",
        "\u4e16\u754c\u89c2",
        "\u7ecf\u5386",
        "\u7231\u5f25\u65af",
        "\u9e23\u6f6e",
    }
    comfort_hints = {
        "\u5b89\u6170",
        "\u966a\u6211",
        "\u96be\u8fc7",
        "\u5bb3\u6015",
        "\u597d\u7d2f",
    }
    memory_hints = {
        "\u8bb0\u5f97",
        "\u8fd8\u8bb0\u5f97",
        "\u56de\u5fc6",
        "\u4e4b\u524d",
    }
    if any(hint in user_input for hint in lore_hints):
        tags.add("lore_answer")
    if any(hint in user_input for hint in comfort_hints):
        tags.add("comfort")
    if any(hint in user_input for hint in memory_hints):
        tags.add("memory_recall")
    if not tags:
        tags.add("casual_chat")
    return tags


def _build_legacy_runtime_context(
    *,
    document: CharacterDocument,
    profile: CharacterProfile,
    context_tags: set[str],
) -> CharacterRuntimeContext:
    card = document.data["card"]
    speech_style = card.get("speaking_style", {})
    tone = speech_style.get("tone", [])
    sentence_length = speech_style.get("sentence_length")
    address_user_as = speech_style.get("address_user_as", [])
    speech_rules = [f"tone: {', '.join(tone)}"] if tone else []
    if sentence_length:
        speech_rules.append(f"sentence_length: {sentence_length}")
    if address_user_as:
        speech_rules.append(f"address_user_as: {', '.join(address_user_as)}")
    if context_tags:
        speech_rules.append(f"context_tags: {', '.join(sorted(context_tags))}")

    personality = card.get("personality", {})
    persona_rules = list(card.get("rules", []))
    for trait in personality.get("traits", []):
        persona_rules.append(f"personality_trait: {trait}")

    identity = card.get("identity", {})
    return CharacterRuntimeContext(
        character_id=profile.character_id,
        display_name=profile.display_name,
        english_name=profile.english_name,
        version=profile.version,
        canon_point=profile.canon_point,
        identity=identity.get("description", ""),
        relation_to_user=identity.get("relation_to_user", ""),
        persona_rules=persona_rules,
        speech_rules=speech_rules,
        boundaries=list(card.get("rules", [])),
        avoid=list(card.get("avoid", [])),
        source_doc_ids=document.source_doc_ids,
    )


def _build_manifest_runtime_context(
    *,
    document: CharacterDocument,
    profile: CharacterProfile,
    context_tags: set[str],
) -> CharacterRuntimeContext:
    identity = _component(document, "identity", "identity")
    relationship = _component(document, "relationship_to_user", "relationship_to_user")
    context_frame_policy = _component(
        document,
        "context_frame_policy",
        "context_frame_policy",
    )
    persona_rules = _component(document, "persona_rules", "persona_rules")
    stable_lore_summary = _component(document, "stable_lore_summary", "stable_lore_summary")
    speech_rules = _component(document, "speech_rules", "speech_rules")
    boundaries = _component(document, "boundaries", "boundaries")
    forbidden_claims = _component(document, "forbidden_claims", "forbidden_claims")
    avoid = _component(document, "avoid", "avoid")
    style_examples = _component(document, "style_examples", "style_examples")

    identity_text = _join_parts(
        [
            identity.get("description", ""),
            *identity.get("known_scope", []),
            *identity.get("unknown_scope", []),
            *identity.get("current_state", []),
        ]
    )
    relation_text = _join_parts(
        [
            f"user_role: {relationship.get('user_role', '')}",
            f"address_user_as: {', '.join(relationship.get('address_user_as', []))}",
            relationship.get("default_stance", ""),
            relationship.get("intimacy_boundary", ""),
            *relationship.get("relationship_constraints", []),
        ]
    )

    context_frame_lines = [context_frame_policy.get("premise", "")]
    for frame in context_frame_policy.get("frames", []):
        context_frame_lines.extend(
            [
                frame.get("description", ""),
                frame.get("handling", ""),
            ]
        )
    context_frame_lines.extend(context_frame_policy.get("constraints", []))

    lore_lines = [
        f"stable_lore_summary: {content}"
        for content in _selected_contents(
            stable_lore_summary,
            context_tags=context_tags,
            default_when_missing=True,
        )
    ]

    return CharacterRuntimeContext(
        character_id=profile.character_id,
        display_name=profile.display_name,
        english_name=profile.english_name,
        version=profile.version,
        canon_point=profile.canon_point,
        identity=identity_text,
        relation_to_user=relation_text,
        persona_rules=[
            *_selected_contents(
                persona_rules,
                context_tags=context_tags,
                default_when_missing=True,
            ),
            *lore_lines,
        ],
        speech_rules=[
            *_selected_contents(
                speech_rules,
                context_tags=context_tags,
                default_when_missing=True,
            ),
            *context_frame_lines,
        ],
        boundaries=_selected_contents(
            boundaries,
            context_tags=context_tags,
            default_when_missing=True,
        ),
        avoid=list(avoid),
        style_examples=_selected_style_examples(style_examples),
        forbidden_claims=_selected_contents(
            forbidden_claims,
            context_tags=context_tags,
            default_when_missing=True,
        ),
        source_doc_ids=document.source_doc_ids,
    )


class CharacterProvider:
    """Provides prompt-ready character context behind a stable interface."""

    def __init__(
        self,
        character_card_path: Path,
        *,
        character_id: str | None = None,
        fallback_path: Path | None = DEFAULT_CHARACTER_FALLBACK_PATH,
    ):
        self.character_card_path = character_card_path
        self.fallback_path = fallback_path
        self._document = load_character_document(character_card_path, fallback_path=fallback_path)
        self._profile = self._build_profile()
        if character_id and character_id != self._profile.character_id:
            raise ValueError(
                f"Character card id mismatch: expected {character_id}, "
                f"got {self._profile.character_id}"
            )

    def _build_profile(self) -> CharacterProfile:
        if self._document.schema_version == "character_manifest.v0.2":
            return _build_manifest_profile(self._document)
        return _build_legacy_profile(self._document.data["card"])

    def load_profile(self, *, character_id: str | None = None) -> CharacterProfile:
        if character_id and character_id != self._profile.character_id:
            raise KeyError(f"Unknown character_id: {character_id}")
        return self._profile

    def build_runtime_context(
        self,
        *,
        character_id: str | None = None,
        user_input: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> CharacterRuntimeContext:
        if character_id and character_id != self._profile.character_id:
            raise KeyError(f"Unknown character_id: {character_id}")
        context_tags = _infer_context_tags(
            user_input=user_input,
            metadata=metadata or {},
        )
        if self._document.schema_version == "character_manifest.v0.2":
            return _build_manifest_runtime_context(
                document=self._document,
                profile=self._profile,
                context_tags=context_tags,
            )
        return _build_legacy_runtime_context(
            document=self._document,
            profile=self._profile,
            context_tags=context_tags,
        )


class CharacterRegistry:
    def __init__(
        self,
        character_card_paths: dict[str, Path],
        *,
        default_character_id: str,
    ):
        if not character_card_paths:
            raise ValueError("CharacterRegistry requires at least one character card.")
        self.default_character_id = default_character_id
        self._providers = {
            character_id: CharacterProvider(path, character_id=character_id)
            for character_id, path in character_card_paths.items()
        }
        if default_character_id not in self._providers:
            raise ValueError(f"Default character is not registered: {default_character_id}")

    @classmethod
    def from_single_card(
        cls,
        *,
        character_card_path: Path,
        default_character_id: str,
    ) -> "CharacterRegistry":
        return cls(
            {default_character_id: character_card_path},
            default_character_id=default_character_id,
        )

    def has_character(self, character_id: str) -> bool:
        return character_id in self._providers

    def list_profiles(self) -> list[CharacterProfile]:
        return [provider.load_profile() for provider in self._providers.values()]

    def load_profile(self, *, character_id: str | None = None) -> CharacterProfile:
        resolved_id = character_id or self.default_character_id
        return self._providers[resolved_id].load_profile(character_id=resolved_id)

    def build_runtime_context(
        self,
        *,
        character_id: str | None = None,
        user_input: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> CharacterRuntimeContext:
        resolved_id = character_id or self.default_character_id
        return self._providers[resolved_id].build_runtime_context(
            character_id=resolved_id,
            user_input=user_input,
            metadata=metadata,
        )
