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
    response_mode: str
    user_input: str
    sections: list[PromptSection]
    metadata: dict[str, Any] = field(default_factory=dict)


class PromptCompiler:
    def compile_messages(self, *, context: PromptContext) -> list[dict[str, str]]:
        system_lines: list[str] = []
        for section in context.sections:
            if section.title:
                system_lines.append(section.title)
            system_lines.extend(section.content_lines)
            system_lines.append("")

        return [
            {"role": "system", "content": "\n".join(system_lines).strip()},
            {"role": "user", "content": context.user_input},
        ]
