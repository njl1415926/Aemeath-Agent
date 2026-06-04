from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.retrieval.schemas import RetrievalResult

DEFAULT_WORLD_INFO_PATH = Path("app/retrieval/data/Aemeath_World_Info.json")


@dataclass(frozen=True)
class WorldInfoEntry:
    uid: int
    comment: str
    content: str
    keys: tuple[str, ...]
    secondary_keys: tuple[str, ...]
    constant: bool
    disabled: bool
    selective: bool
    selective_logic: int
    position: int | str
    order: int
    depth: int
    probability: int

    @classmethod
    def from_mapping(cls, uid: int, data: dict[str, Any]) -> WorldInfoEntry:
        return cls(
            uid=uid,
            comment=str(data.get("comment") or ""),
            content=str(data.get("content") or ""),
            keys=_string_tuple(data.get("key") or data.get("keys")),
            secondary_keys=_string_tuple(data.get("keysecondary") or data.get("secondary_keys")),
            constant=bool(data.get("constant", False)),
            disabled=bool(data.get("disable", False)),
            selective=bool(data.get("selective", False)),
            selective_logic=_int_or_default(data.get("selectiveLogic"), 0),
            position=data.get("position", 0),
            order=_int_or_default(data.get("order"), 100),
            depth=_int_or_default(data.get("depth"), 0),
            probability=_int_or_default(data.get("probability"), 100),
        )


@dataclass(frozen=True)
class WorldInfoMatch:
    entry: WorldInfoEntry
    matched_keys: tuple[str, ...]
    matched_secondary_keys: tuple[str, ...] = ()

    @property
    def confidence(self) -> float:
        if self.entry.constant:
            return 1.0
        return min(
            1.0,
            0.55 + 0.15 * len(self.matched_keys) + 0.1 * len(self.matched_secondary_keys),
        )


class WorldInfoLoader:
    def __init__(self, path: Path = DEFAULT_WORLD_INFO_PATH):
        self.path = path

    def load_entries(self) -> list[WorldInfoEntry]:
        data = json.loads(self.path.read_text(encoding="utf-8"))
        entries_data = data.get("entries", {})
        if not isinstance(entries_data, dict):
            raise ValueError("World Info JSON must contain top-level object field 'entries'.")

        entries: list[WorldInfoEntry] = []
        for uid_text, entry_data in entries_data.items():
            if not isinstance(entry_data, dict):
                continue
            uid = _int_or_default(entry_data.get("uid"), _int_or_default(uid_text, -1))
            if uid < 0:
                continue
            entry = WorldInfoEntry.from_mapping(uid, entry_data)
            if entry.disabled or not entry.content.strip():
                continue
            entries.append(entry)
        return entries


class WorldInfoKeywordMatcher:
    def match(
        self,
        *,
        entries: list[WorldInfoEntry],
        scan_text: str,
        include_constants: bool = False,
    ) -> tuple[list[WorldInfoMatch], list[WorldInfoMatch]]:
        constant_matches: list[WorldInfoMatch] = []
        keyword_matches: list[WorldInfoMatch] = []

        for entry in entries:
            if entry.constant:
                if include_constants:
                    constant_matches.append(WorldInfoMatch(entry=entry, matched_keys=()))
                continue

            matched_keys = _match_key_list(entry.keys, scan_text)
            if not matched_keys:
                continue

            secondary_matches = _match_key_list(entry.secondary_keys, scan_text)
            if not self._secondary_allows_entry(entry, secondary_matches):
                continue

            keyword_matches.append(
                WorldInfoMatch(
                    entry=entry,
                    matched_keys=tuple(matched_keys),
                    matched_secondary_keys=tuple(secondary_matches),
                )
            )

        constant_matches.sort(key=lambda match: (match.entry.order, match.entry.uid))
        keyword_matches.sort(
            key=lambda match: (
                -len(match.matched_keys),
                match.entry.order,
                match.entry.uid,
            )
        )
        return constant_matches, keyword_matches

    def _secondary_allows_entry(
        self,
        entry: WorldInfoEntry,
        secondary_matches: list[str],
    ) -> bool:
        if not entry.selective or not entry.secondary_keys:
            return True

        match_count = len(secondary_matches)
        total_count = len(entry.secondary_keys)
        if entry.selective_logic == 0:
            return match_count > 0
        if entry.selective_logic == 1:
            return match_count == total_count
        if entry.selective_logic == 2:
            return match_count == 0
        if entry.selective_logic == 3:
            return match_count != total_count
        return True


class WorldInfoKeywordRetriever:
    def __init__(
        self,
        *,
        loader: WorldInfoLoader | None = None,
        matcher: WorldInfoKeywordMatcher | None = None,
    ):
        self.loader = loader or WorldInfoLoader()
        self.matcher = matcher or WorldInfoKeywordMatcher()
        self._entries: list[WorldInfoEntry] | None = None

    def retrieve(
        self,
        *,
        query: str,
        user_id: str | None = None,
        namespaces: list[str] | None = None,
        limit: int = 5,
        metadata: dict[str, Any] | None = None,
    ) -> list[RetrievalResult]:
        metadata = metadata or {}
        include_constants = bool(metadata.get("include_world_info_constants", False))
        constant_limit = metadata.get("constant_limit")
        constant_limit = None if constant_limit is None else _int_or_default(constant_limit, 0)
        constant_uids = _int_set(metadata.get("world_info_policy_constant_uids"))
        scan_text = _build_scan_text(query=query, metadata=metadata)

        constant_matches, keyword_matches = self.matcher.match(
            entries=self._load_entries(),
            scan_text=scan_text,
            include_constants=include_constants,
        )

        allowed_namespaces = set(namespaces or [])
        results: list[RetrievalResult] = []
        if include_constants:
            if constant_uids:
                constant_matches = [
                    match for match in constant_matches if match.entry.uid in constant_uids
                ]
            selected_constants = (
                constant_matches if constant_limit is None else constant_matches[:constant_limit]
            )
            results.extend(
                self._to_result(match, namespace="world:constant") for match in selected_constants
            )

        selected_keywords = keyword_matches[:limit]
        results.extend(
            self._to_result(match, namespace="world:keyword") for match in selected_keywords
        )

        if allowed_namespaces:
            results = [item for item in results if item.namespace in allowed_namespaces]
        return results

    def _load_entries(self) -> list[WorldInfoEntry]:
        if self._entries is None:
            self._entries = self.loader.load_entries()
        return self._entries

    def _to_result(self, match: WorldInfoMatch, *, namespace: str) -> RetrievalResult:
        entry = match.entry
        return RetrievalResult(
            id=f"world_info:{entry.uid}",
            namespace=namespace,
            content=entry.content,
            source_type="world_info_entry",
            confidence=match.confidence,
            metadata={
                "retriever": "world_info_keyword",
                "uid": entry.uid,
                "comment": entry.comment,
                "matched_keys": list(match.matched_keys),
                "matched_secondary_keys": list(match.matched_secondary_keys),
                "constant": entry.constant,
                "position": entry.position,
                "order": entry.order,
                "depth": entry.depth,
                "probability": entry.probability,
                "source_path": str(self.loader.path),
            },
        )


def _build_scan_text(*, query: str, metadata: dict[str, Any]) -> str:
    parts = [query]
    recent_messages = metadata.get("recent_messages") or []
    if isinstance(recent_messages, list):
        for message in recent_messages:
            if isinstance(message, str):
                parts.append(message)
            elif isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, str):
                    parts.append(content)
    return "\n".join(part for part in parts if part)


def _match_key_list(keys: tuple[str, ...], scan_text: str) -> list[str]:
    return [key for key in keys if _key_matches(key, scan_text)]


def _key_matches(key: str, scan_text: str) -> bool:
    if not key:
        return False
    regex_pattern = _extract_regex_pattern(key)
    if regex_pattern:
        try:
            return re.search(regex_pattern, scan_text) is not None
        except re.error:
            return False
    return key in scan_text


def _extract_regex_pattern(key: str) -> str | None:
    if len(key) < 2 or not key.startswith("/"):
        return None
    end_index = key.rfind("/")
    if end_index <= 0:
        return None
    return key[1:end_index]


def _string_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, list):
        return tuple(str(item) for item in value if str(item))
    if isinstance(value, str) and value:
        return (value,)
    return ()


def _int_or_default(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _int_set(value: Any) -> set[int]:
    if value is None:
        return set()
    if isinstance(value, (str, int)):
        return {_int_or_default(value, -1)} - {-1}
    if isinstance(value, (list, tuple, set)):
        return {_int_or_default(item, -1) for item in value} - {-1}
    return set()
