from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class PromptSection:
    id: str
    title: str
    content_lines: list[str]
    source_ids: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PromptContext:
    user_input: str
    sections: list[PromptSection]
    metadata: dict[str, Any] = field(default_factory=dict)


def render_system_content(*, context: PromptContext) -> str:
    system_lines: list[str] = []
    for section in context.sections:
        if section.title:
            system_lines.append(section.title)
        system_lines.extend(section.content_lines)
        system_lines.append("")
    return "\n".join(system_lines).strip()


def estimate_prompt_chars(*, context: PromptContext) -> int:
    return len(render_system_content(context=context)) + len(context.user_input)


class PromptCompiler:
    def compile_messages(self, *, context: PromptContext) -> list[dict[str, str]]:
        return [
            {"role": "system", "content": render_system_content(context=context)},
            {"role": "user", "content": context.user_input},
        ]
