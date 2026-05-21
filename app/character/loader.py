from __future__ import annotations

from pathlib import Path

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


def load_character_card(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"Character card not found: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Character card must be a mapping object.")
    missing = [field for field in REQUIRED_CHARACTER_FIELDS if field not in data]
    if missing:
        raise ValueError(f"Character card missing required fields: {', '.join(sorted(missing))}")
    return data

