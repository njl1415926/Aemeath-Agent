from __future__ import annotations

from typing import Protocol


class ChatModelClient(Protocol):
    def chat(self, *, messages: list[dict[str, str]]) -> str:
        ...

