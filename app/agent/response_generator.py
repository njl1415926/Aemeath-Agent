from __future__ import annotations

from app.model.client import ChatModelClient


class ResponseGenerator:
    def __init__(self, chat_model: ChatModelClient):
        self.chat_model = chat_model

    def generate(
        self,
        *,
        messages: list[dict[str, str]],
        user_input: str,
        retrieved_memories: list[str],
    ) -> str:
        return self.chat_model.chat(
            messages=messages,
        )
