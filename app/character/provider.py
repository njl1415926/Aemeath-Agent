from __future__ import annotations

from pathlib import Path

from app.character.loader import load_character_card
from app.character.schemas import CharacterProfile, CharacterRuntimeContext


class CharacterProvider:
    """Provides prompt-ready character context behind a stable interface."""

    def __init__(self, character_card_path: Path):
        self.character_card_path = character_card_path
        self._card = load_character_card(character_card_path)
        self._profile = self._build_profile(self._card)

    def _build_profile(self, card: dict) -> CharacterProfile:
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

    def load_profile(self) -> CharacterProfile:
        return self._profile

    def build_runtime_context(self, *, response_mode: str) -> CharacterRuntimeContext:
        speech_style = self._card.get("speaking_style", {})
        tone = speech_style.get("tone", [])
        sentence_length = speech_style.get("sentence_length")
        address_user_as = speech_style.get("address_user_as", [])
        speech_rules = [f"tone: {', '.join(tone)}"] if tone else []
        if sentence_length:
            speech_rules.append(f"sentence_length: {sentence_length}")
        if address_user_as:
            speech_rules.append(f"address_user_as: {', '.join(address_user_as)}")
        speech_rules.append(f"response_mode: {response_mode}")

        personality = self._card.get("personality", {})
        persona_rules = list(self._card.get("rules", []))
        for trait in personality.get("traits", []):
            persona_rules.append(f"personality_trait: {trait}")

        identity = self._card.get("identity", {})
        return CharacterRuntimeContext(
            character_id=self._profile.character_id,
            display_name=self._profile.display_name,
            english_name=self._profile.english_name,
            version=self._profile.version,
            canon_point=self._profile.canon_point,
            identity=identity.get("description", ""),
            relation_to_user=identity.get("relation_to_user", ""),
            persona_rules=persona_rules,
            speech_rules=speech_rules,
            boundaries=list(self._card.get("rules", [])),
            avoid=list(self._card.get("avoid", [])),
            source_doc_ids=[str(self.character_card_path)],
        )

