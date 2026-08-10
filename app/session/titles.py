from __future__ import annotations

import re
import unicodedata

from app.errors import AemeathError, ErrorCode

_MAX_TITLE_CODEPOINTS = 80
_MAX_TITLE_BYTES = 256
_WHITESPACE_RE = re.compile(r"\s+", re.UNICODE)


def normalize_session_title(title: str | None) -> str | None:
    if title is None:
        return None
    normalized = unicodedata.normalize("NFC", title)
    if any(_is_disallowed_control(character) for character in normalized):
        raise AemeathError(
            code=ErrorCode.INVALID_INPUT,
            public_message="Invalid session title.",
            internal_message="session_title_contains_control_character",
        )
    collapsed = _WHITESPACE_RE.sub(" ", normalized).strip()
    if not collapsed:
        return None
    if len(collapsed) > _MAX_TITLE_CODEPOINTS:
        raise AemeathError(
            code=ErrorCode.INVALID_INPUT,
            public_message="Invalid session title.",
            internal_message="session_title_too_long",
            metadata={"max_codepoints": _MAX_TITLE_CODEPOINTS},
        )
    byte_length = len(collapsed.encode("utf-8"))
    if byte_length > _MAX_TITLE_BYTES:
        raise AemeathError(
            code=ErrorCode.INVALID_INPUT,
            public_message="Invalid session title.",
            internal_message="session_title_too_many_bytes",
            metadata={"max_bytes": _MAX_TITLE_BYTES},
        )
    return collapsed


def _is_disallowed_control(character: str) -> bool:
    codepoint = ord(character)
    return (0 <= codepoint <= 0x1F or 0x7F <= codepoint <= 0x9F) and not character.isspace()
