from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class CharacterProfile:
    character_id: str
    display_name: str
    english_name: str
    source: str
    version: str
    canon_point: str
    roleplay_pack_path: Path | None = None


@dataclass(frozen=True)
class CharacterRuntimeContext:
    character_id: str
    display_name: str
    english_name: str
    version: str
    canon_point: str
    identity: str
    relation_to_user: str
    persona_rules: list[str] = field(default_factory=list)
    speech_rules: list[str] = field(default_factory=list)
    boundaries: list[str] = field(default_factory=list)
    avoid: list[str] = field(default_factory=list)
    style_examples: list[str] = field(default_factory=list)
    forbidden_claims: list[str] = field(default_factory=list)
    source_doc_ids: list[str] = field(default_factory=list)

