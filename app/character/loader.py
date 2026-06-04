from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

REQUIRED_CHARACTER_FIELDS = {
    "name",
    "english_name",
    "source",
    "version",
    "identity",
    "speaking_style",
    "personality",
    "rules",
    "avoid",
}

DEFAULT_CHARACTER_MANIFEST_PATH = Path("app/character/Aemeath.v0.2")
DEFAULT_CHARACTER_FALLBACK_PATH = Path("app/character/Aemeath.v0.1/character.yaml")


def _path_id(path: Path) -> str:
    return path.as_posix()


@dataclass(frozen=True)
class CharacterDocument:
    schema_version: str
    source_path: Path
    data: dict[str, Any]
    fallback_metadata: dict[str, str] | None = None
    source_doc_ids: list[str] = field(default_factory=list)


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Character card not found: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("Character card must be a mapping object.")
    return data


def load_character_card(path: Path) -> dict[str, Any]:
    """Load a legacy v0.1 single-file character card."""
    data = _read_yaml(path)
    missing = [field for field in REQUIRED_CHARACTER_FIELDS if field not in data]
    if missing:
        raise ValueError(f"Character card missing required fields: {', '.join(sorted(missing))}")
    return data


def _resolve_manifest_path(path: Path) -> Path:
    return path / "manifest.yaml" if path.is_dir() else path


def load_character_manifest(path: Path) -> CharacterDocument:
    manifest_path = _resolve_manifest_path(path)
    manifest = _read_yaml(manifest_path)
    if manifest.get("schema_version") != "character_manifest.v0.2":
        raise ValueError(f"Unsupported character manifest schema: {manifest.get('schema_version')}")
    components = manifest.get("components")
    if not isinstance(components, dict):
        raise ValueError("Character manifest missing components mapping.")

    base_dir = manifest_path.parent
    loaded_components: dict[str, Any] = {}
    source_doc_ids = [_path_id(manifest_path)]
    for name, rel_path in components.items():
        component_path = base_dir / str(rel_path)
        loaded_components[name] = _read_yaml(component_path)
        source_doc_ids.append(_path_id(component_path))

    return CharacterDocument(
        schema_version="character_manifest.v0.2",
        source_path=manifest_path,
        data={
            "manifest": manifest,
            "components": loaded_components,
        },
        source_doc_ids=source_doc_ids,
    )


def load_character_document(
    path: Path,
    *,
    fallback_path: Path | None = DEFAULT_CHARACTER_FALLBACK_PATH,
) -> CharacterDocument:
    """Load a character document, preferring v0.2 manifests and falling back to v0.1.

    Fallback is intentionally visible through CharacterDocument.fallback_metadata and
    source_doc_ids so the provider can expose it without changing the public port.
    """
    try:
        schema_version = _read_yaml(_resolve_manifest_path(path)).get("schema_version")
        if path.is_dir() or schema_version == "character_manifest.v0.2":
            return load_character_manifest(path)
        card = load_character_card(path)
        return CharacterDocument(
            schema_version="character_card.v0.1",
            source_path=path,
            data={"card": card},
            source_doc_ids=[_path_id(path)],
        )
    except Exception as exc:
        if fallback_path is None:
            raise
        fallback_card = load_character_card(fallback_path)
        error_type = type(exc).__name__
        return CharacterDocument(
            schema_version="character_card.v0.1",
            source_path=fallback_path,
            data={"card": fallback_card},
            fallback_metadata={
                "event": "character_source_fallback",
                "primary_path": _path_id(path),
                "fallback_path": _path_id(fallback_path),
                "error_type": error_type,
            },
            source_doc_ids=[
                "character_source_fallback",
                f"primary_path={_path_id(path)}",
                f"fallback_path={_path_id(fallback_path)}",
                f"error_type={error_type}",
                _path_id(fallback_path),
            ],
        )
