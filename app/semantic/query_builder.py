from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from app.embeddings.artifacts import canonicalize_embedding_text
from app.semantic.budget import SemanticQueryBudget
from app.semantic.schemas import (
    SemanticMessage,
    SemanticQueryStrategy,
    SemanticTurnInput,
)

SEMANTIC_QUERY_CONSTRUCTION_VERSION = "turn_neutral_v0_1"
_TRUNCATION_MARKER = "\n[semantic_context_truncated]\n"

_CONTEXT_PREFIXES = (
    "那",
    "那么",
    "然后",
    "所以",
    "继续",
    "还有",
    "再说",
    "这个",
    "那个",
    "它",
    "他",
    "她",
    "他们",
    "她们",
    "这些",
    "那些",
)
_CONTEXT_REFERENCES = (
    "刚才",
    "前面",
    "你说的",
    "我说的",
    "我们聊的",
    "上一个",
)
_SHORT_FOLLOW_UPS = {
    "为什么",
    "怎么会",
    "怎么办",
    "后来呢",
    "然后呢",
    "那呢",
    "还有呢",
}
_ENGLISH_PREFIXES = ("then", "so", "continue", "what about", "why")
_ENGLISH_REFERENCES = ("you said", "we discussed")


@dataclass(frozen=True)
class BuiltSemanticQuery:
    status: Literal["ready", "skipped"]
    reason_code: str
    construction_version: str
    budget_version: str
    query_strategy: SemanticQueryStrategy
    canonical_query: str | None = field(repr=False)
    dependency_message_ids: tuple[str, ...] = field(default=(), repr=False)
    budget_reason_codes: tuple[str, ...] = ()
    query_char_count: int = 0
    query_utf8_bytes: int = 0


@dataclass(frozen=True)
class _DependencyBlock:
    message_id: str
    role: Literal["user", "assistant"]
    content: str = field(repr=False)


class NeutralTurnQueryBuilderV01:
    construction_version = SEMANTIC_QUERY_CONSTRUCTION_VERSION

    def build(
        self,
        *,
        turn_input: SemanticTurnInput,
        budget: SemanticQueryBudget,
    ) -> BuiltSemanticQuery:
        if not isinstance(turn_input.current_input, str):
            raise ValueError("semantic current input must be a string")
        current = _normalize(turn_input.current_input)
        if not current:
            return BuiltSemanticQuery(
                status="skipped",
                reason_code="blank_current_input",
                construction_version=self.construction_version,
                budget_version=budget.version,
                query_strategy="none",
                canonical_query=None,
            )

        current_chars, current_bytes = _measure(current)
        if _exceeds_total_budget(current, budget):
            return BuiltSemanticQuery(
                status="skipped",
                reason_code="current_input_exceeds_semantic_budget",
                construction_version=self.construction_version,
                budget_version=budget.version,
                query_strategy="current_only",
                canonical_query=None,
                query_char_count=current_chars,
                query_utf8_bytes=current_bytes,
            )

        dependencies = (
            self._select_dependencies(turn_input) if _depends_on_context(current) else []
        )
        budget_reasons: list[str] = []
        budgeted: list[_DependencyBlock] = []
        for dependency in dependencies:
            content, truncated = _budget_dependency(dependency.content, budget)
            if truncated:
                _append_unique(
                    budget_reasons,
                    "dependency_truncated_to_semantic_budget",
                )
            budgeted.append(
                _DependencyBlock(
                    message_id=dependency.message_id,
                    role=dependency.role,
                    content=content,
                )
            )

        query = _compose_query(budgeted, current)
        while budgeted and _exceeds_total_budget(query, budget):
            budgeted.pop(0)
            _append_unique(
                budget_reasons,
                "dependency_dropped_for_total_semantic_budget",
            )
            query = _compose_query(budgeted, current)

        if _exceeds_total_budget(query, budget):
            return BuiltSemanticQuery(
                status="skipped",
                reason_code="current_input_exceeds_semantic_budget",
                construction_version=self.construction_version,
                budget_version=budget.version,
                query_strategy="current_only",
                canonical_query=None,
                budget_reason_codes=tuple(budget_reasons),
                query_char_count=current_chars,
                query_utf8_bytes=current_bytes,
            )

        query = canonicalize_embedding_text(query).strip()
        query_chars, query_bytes = _measure(query)
        return BuiltSemanticQuery(
            status="ready",
            reason_code="query_ready",
            construction_version=self.construction_version,
            budget_version=budget.version,
            query_strategy=_query_strategy(budgeted),
            canonical_query=query,
            dependency_message_ids=tuple(item.message_id for item in budgeted),
            budget_reason_codes=tuple(budget_reasons),
            query_char_count=query_chars,
            query_utf8_bytes=query_bytes,
        )

    def _select_dependencies(self, turn_input: SemanticTurnInput) -> list[_DependencyBlock]:
        usable: list[_DependencyBlock] = []
        for message in turn_input.recent_messages:
            if not isinstance(message, SemanticMessage):
                raise ValueError("semantic recent messages must be SemanticMessage values")
            if message.id == turn_input.current_user_message_id:
                continue
            content = _normalize(message.content)
            if not content:
                continue
            usable.append(
                _DependencyBlock(
                    message_id=message.id,
                    role=message.role,
                    content=content,
                )
            )
        if not usable:
            return []
        latest = usable[-1]
        if latest.role == "user":
            return [latest]
        for candidate in reversed(usable[:-1]):
            if candidate.role == "user":
                return [candidate, latest]
        return [latest]


def _normalize(text: str) -> str:
    return canonicalize_embedding_text(text).strip()


def _depends_on_context(text: str) -> bool:
    stripped = text.strip()
    lowered = stripped.casefold()
    compact = stripped.strip("。！？!?… .\t\r\n").casefold()
    if stripped.startswith(_CONTEXT_PREFIXES):
        return True
    if any(reference in stripped for reference in _CONTEXT_REFERENCES):
        return True
    if compact in _SHORT_FOLLOW_UPS:
        return True
    if any(
        lowered == prefix or lowered.startswith(f"{prefix} ")
        for prefix in _ENGLISH_PREFIXES
    ):
        return True
    return any(reference in lowered for reference in _ENGLISH_REFERENCES)


def _budget_dependency(
    text: str,
    budget: SemanticQueryBudget,
) -> tuple[str, bool]:
    if (
        len(text) <= budget.max_dependency_chars_per_message
        and len(text.encode("utf-8")) <= budget.max_dependency_utf8_bytes_per_message
    ):
        return text, False

    available_chars = budget.max_dependency_chars_per_message - len(_TRUNCATION_MARKER)
    head_size = (available_chars + 1) // 2
    tail_size = available_chars // 2
    head = text[:head_size]
    tail = text[-tail_size:] if tail_size else ""
    while len((head + _TRUNCATION_MARKER + tail).encode("utf-8")) > (
        budget.max_dependency_utf8_bytes_per_message
    ):
        if not head and not tail:
            break
        head_bytes = len(head.encode("utf-8"))
        tail_bytes = len(tail.encode("utf-8"))
        if head_bytes > tail_bytes and head:
            head = head[:-1]
        elif tail:
            tail = tail[1:]
        else:
            head = head[:-1]
    return head + _TRUNCATION_MARKER + tail, True


def _compose_query(dependencies: list[_DependencyBlock], current: str) -> str:
    if not dependencies:
        return current
    lines: list[str] = []
    for dependency in dependencies:
        lines.extend((f"[recent_{dependency.role}]", dependency.content))
    lines.extend(("[current_user]", current))
    return "\n".join(lines)


def _query_strategy(dependencies: list[_DependencyBlock]) -> SemanticQueryStrategy:
    roles = tuple(item.role for item in dependencies)
    if roles == ("user", "assistant"):
        return "last_turn_pair_and_current"
    if roles == ("user",):
        return "recent_user_and_current"
    if roles == ("assistant",):
        return "recent_assistant_and_current"
    return "current_only"


def _measure(text: str) -> tuple[int, int]:
    return len(text), len(text.encode("utf-8"))


def _exceeds_total_budget(text: str, budget: SemanticQueryBudget) -> bool:
    char_count, byte_count = _measure(text)
    return char_count > budget.max_query_chars or byte_count > budget.max_query_utf8_bytes


def _append_unique(values: list[str], value: str) -> None:
    if value not in values:
        values.append(value)
