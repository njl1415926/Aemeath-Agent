from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session

SessionFactory = Callable[[], Session]


def dump_json(value: Any, *, sort_keys: bool = False, ensure_ascii: bool = False) -> str:
    return json.dumps(value, ensure_ascii=ensure_ascii, sort_keys=sort_keys)


def load_json_object(raw: str | None) -> dict[str, Any]:
    value = json.loads(raw or "{}")
    if not isinstance(value, dict):
        raise ValueError("stored JSON value must be an object")
    return value


def load_json_list(raw: str | None) -> list[Any]:
    value = json.loads(raw or "[]")
    if not isinstance(value, list):
        raise ValueError("stored JSON value must be a list")
    return value


def clamp_stat(value: int) -> int:
    return max(0, min(100, value))
