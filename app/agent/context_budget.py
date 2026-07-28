from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
from typing import Any

from app.agent.prompt_context import PromptContext, PromptSection, estimate_prompt_chars

CONTEXT_BUDGET_POLICY_VERSION = "context-budget-policy/v1"
CONTEXT_BUDGET_REPORT_SCHEMA_VERSION = "context-budget-report/v1"

_TRIMMABLE_SECTION_ORDER = (
    "style_examples",
    "voice_evidence",
    "canon_evidence",
    "interaction_memories",
    "recent_conversation",
)
_TRIMMABLE_SECTION_IDS = frozenset(_TRIMMABLE_SECTION_ORDER)
_PROTECTED_SECTION_IDS = frozenset(
    {
        "base_instructions",
        "character_context",
        "persona_rules",
        "speech_rules",
        "boundaries",
        "forbidden_claims",
        "avoid",
        "relationship_state",
    }
)


@dataclass(frozen=True)
class ContextBudgetPolicy:
    enabled: bool = False
    max_chars: int = 24_000
    recent_message_limit: int = 8
    memory_limit: int = 4
    evidence_limit: int = 4
    version: str = CONTEXT_BUDGET_POLICY_VERSION

    def __post_init__(self) -> None:
        if self.max_chars <= 0:
            raise ValueError("context budget max_chars must be positive")
        for name, value in (
            ("recent_message_limit", self.recent_message_limit),
            ("memory_limit", self.memory_limit),
            ("evidence_limit", self.evidence_limit),
        ):
            if value < 0:
                raise ValueError(f"context budget {name} must be non-negative")


class NoopContextCompressor:
    def compress(self, *, context: PromptContext) -> PromptContext:
        return context


class DeterministicContextCompressor:
    def __init__(self, *, policy: ContextBudgetPolicy):
        self.policy = policy

    def compress(self, *, context: PromptContext) -> PromptContext:
        estimated_before = estimate_prompt_chars(context=context)
        if not self.policy.enabled:
            return self._with_report(
                context=context,
                report=self._disabled_report(estimated_chars=estimated_before),
            )

        working = [_copy_section(section) for section in context.sections]
        original_sections = {section.id: section for section in context.sections}
        duplicate_section_ids = _duplicate_section_ids(context.sections)
        invalid_item_mapping_section_ids = [
            section.id
            for section in context.sections
            if section.id in _TRIMMABLE_SECTION_IDS and not _has_valid_item_mapping(section)
        ]
        unclassified_section_ids = [
            section.id
            for section in context.sections
            if section.id not in _PROTECTED_SECTION_IDS and section.id not in _TRIMMABLE_SECTION_IDS
        ]
        unsafe_to_trim = set(duplicate_section_ids) | set(invalid_item_mapping_section_ids)

        dropped_source_ids: dict[str, list[str]] = defaultdict(list)
        drop_reasons: dict[str, set[str]] = defaultdict(set)
        changed_section_ids: set[str] = set()

        for section_id, limit, keep_latest in (
            ("recent_conversation", self.policy.recent_message_limit, True),
            ("interaction_memories", self.policy.memory_limit, False),
            ("canon_evidence", self.policy.evidence_limit, False),
            ("voice_evidence", self.policy.evidence_limit, False),
        ):
            if section_id in unsafe_to_trim:
                continue
            index = _find_section_index(working, section_id)
            if index is None:
                continue
            section = working[index]
            if len(section.source_ids) <= limit:
                continue
            if keep_latest:
                kept_lines = section.content_lines[-limit:] if limit else []
                kept_ids = section.source_ids[-limit:] if limit else []
                removed_ids = section.source_ids[:-limit] if limit else section.source_ids
            else:
                kept_lines = section.content_lines[:limit]
                kept_ids = section.source_ids[:limit]
                removed_ids = section.source_ids[limit:]
            dropped_source_ids[section_id].extend(removed_ids)
            drop_reasons[section_id].add("section_limit")
            changed_section_ids.add(section_id)
            if kept_ids:
                working[index] = replace(
                    section,
                    content_lines=list(kept_lines),
                    source_ids=list(kept_ids),
                )
            else:
                working.pop(index)

        after_limits_context = PromptContext(
            user_input=context.user_input,
            sections=working,
            metadata=context.metadata,
        )
        estimated_after_limits = estimate_prompt_chars(context=after_limits_context)

        while True:
            working_context = PromptContext(
                user_input=context.user_input,
                sections=working,
                metadata=context.metadata,
            )
            if estimate_prompt_chars(context=working_context) <= self.policy.max_chars:
                break
            candidate = self._next_global_candidate(
                sections=working,
                unsafe_to_trim=unsafe_to_trim,
            )
            if candidate is None:
                break
            section_index, source_index = candidate
            section = working[section_index]
            section_id = section.id
            changed_section_ids.add(section_id)
            drop_reasons[section_id].add("global_char_budget")
            if source_index is None:
                working.pop(section_index)
                continue
            dropped_source_ids[section_id].append(section.source_ids[source_index])
            kept_lines = list(section.content_lines)
            kept_ids = list(section.source_ids)
            kept_lines.pop(source_index)
            kept_ids.pop(source_index)
            if kept_ids:
                working[section_index] = replace(
                    section,
                    content_lines=kept_lines,
                    source_ids=kept_ids,
                )
            else:
                working.pop(section_index)

        final_context = PromptContext(
            user_input=context.user_input,
            sections=working,
            metadata=context.metadata,
        )
        estimated_after = estimate_prompt_chars(context=final_context)
        final_section_ids = [section.id for section in working]
        final_section_id_set = set(final_section_ids)
        dropped_section_ids = [
            section.id
            for section in context.sections
            if section.id in changed_section_ids and section.id not in final_section_id_set
        ]
        trimmed_section_ids = [
            section.id
            for section in context.sections
            if section.id in changed_section_ids and section.id in final_section_id_set
        ]
        kept_source_ids = {
            section.id: list(section.source_ids) for section in working if section.source_ids
        }
        ordered_dropped_source_ids = {
            section.id: _ordered_subset(
                original_sections[section.id].source_ids,
                dropped_source_ids[section.id],
            )
            for section in context.sections
            if section.id in dropped_source_ids and dropped_source_ids[section.id]
        }
        trimmed_sections = []
        for section in context.sections:
            if section.id not in changed_section_ids:
                continue
            final_section = next((item for item in working if item.id == section.id), None)
            trimmed_sections.append(
                {
                    "section_id": section.id,
                    "before_lines": len(section.content_lines),
                    "after_lines": len(final_section.content_lines) if final_section else 0,
                    "dropped_line_count": len(section.content_lines)
                    - (len(final_section.content_lines) if final_section else 0),
                    "drop_reasons": [
                        reason
                        for reason in ("section_limit", "global_char_budget")
                        if reason in drop_reasons[section.id]
                    ],
                }
            )

        over_budget_chars = max(0, estimated_after - self.policy.max_chars)
        if over_budget_chars:
            status = "protected_floor_exceeded"
        elif changed_section_ids:
            status = "trimmed"
        else:
            status = "within_budget"
        report: dict[str, Any] = {
            "schema_version": CONTEXT_BUDGET_REPORT_SCHEMA_VERSION,
            "policy_version": self.policy.version,
            "budget_enabled": True,
            "budget_limit": self.policy.max_chars,
            "budget_unit": "chars",
            "estimator": "compiled_message_chars_v1",
            "status": status,
            "applied": bool(changed_section_ids),
            "estimated_before": estimated_before,
            "estimated_after_limits": estimated_after_limits,
            "estimated_after": estimated_after,
            "over_budget_chars": over_budget_chars,
            "kept_section_ids": final_section_ids,
            "trimmed_section_ids": trimmed_section_ids,
            "dropped_section_ids": dropped_section_ids,
            "kept_source_ids": kept_source_ids,
            "dropped_source_ids": ordered_dropped_source_ids,
            "trimmed_sections": trimmed_sections,
            "dropped_line_count": sum(item["dropped_line_count"] for item in trimmed_sections),
            "limits": {
                "recent_message_limit": self.policy.recent_message_limit,
                "memory_limit": self.policy.memory_limit,
                "evidence_limit_per_section": self.policy.evidence_limit,
            },
            "unclassified_section_ids": unclassified_section_ids,
            "invalid_item_mapping_section_ids": invalid_item_mapping_section_ids,
            "duplicate_section_ids": duplicate_section_ids,
        }
        return self._with_report(context=final_context, report=report)

    def _next_global_candidate(
        self,
        *,
        sections: list[PromptSection],
        unsafe_to_trim: set[str],
    ) -> tuple[int, int | None] | None:
        for section_id in _TRIMMABLE_SECTION_ORDER:
            if section_id in unsafe_to_trim:
                continue
            index = _find_section_index(sections, section_id)
            if index is None:
                continue
            section = sections[index]
            if not section.source_ids:
                return index, None
            source_index = 0 if section_id == "recent_conversation" else -1
            return index, source_index
        return None

    def _disabled_report(self, *, estimated_chars: int) -> dict[str, Any]:
        return {
            "schema_version": CONTEXT_BUDGET_REPORT_SCHEMA_VERSION,
            "policy_version": self.policy.version,
            "budget_enabled": False,
            "budget_limit": self.policy.max_chars,
            "budget_unit": "chars",
            "estimator": "compiled_message_chars_v1",
            "status": "disabled",
            "applied": False,
            "estimated_before": estimated_chars,
            "estimated_after_limits": estimated_chars,
            "estimated_after": estimated_chars,
            "over_budget_chars": max(0, estimated_chars - self.policy.max_chars),
            "trimmed_section_ids": [],
            "dropped_section_ids": [],
            "dropped_source_ids": {},
            "dropped_line_count": 0,
            "limits": {
                "recent_message_limit": self.policy.recent_message_limit,
                "memory_limit": self.policy.memory_limit,
                "evidence_limit_per_section": self.policy.evidence_limit,
            },
        }

    def _with_report(
        self,
        *,
        context: PromptContext,
        report: dict[str, Any],
    ) -> PromptContext:
        return PromptContext(
            user_input=context.user_input,
            sections=context.sections,
            metadata={**context.metadata, "context_budget": report},
        )


def _copy_section(section: PromptSection) -> PromptSection:
    return PromptSection(
        id=section.id,
        title=section.title,
        content_lines=list(section.content_lines),
        source_ids=list(section.source_ids),
        metadata=dict(section.metadata),
    )


def _find_section_index(sections: list[PromptSection], section_id: str) -> int | None:
    for index, section in enumerate(sections):
        if section.id == section_id:
            return index
    return None


def _has_valid_item_mapping(section: PromptSection) -> bool:
    if not section.source_ids:
        return len(section.content_lines) <= 1
    return len(section.source_ids) == len(section.content_lines)


def _duplicate_section_ids(sections: list[PromptSection]) -> list[str]:
    seen: set[str] = set()
    duplicates: list[str] = []
    for section in sections:
        if section.id in seen and section.id not in duplicates:
            duplicates.append(section.id)
        seen.add(section.id)
    return duplicates


def _ordered_subset(original: list[str], selected: list[str]) -> list[str]:
    remaining = list(selected)
    output: list[str] = []
    for source_id in original:
        if source_id not in remaining:
            continue
        output.append(source_id)
        remaining.remove(source_id)
    output.extend(remaining)
    return output
