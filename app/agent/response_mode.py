from __future__ import annotations

from typing import Literal

ResponseMode = Literal[
    "casual_chat",
    "comfort",
    "memory_recall",
    "planning",
    "lore_answer",
    "technical_help",
    "roleplay_scene",
]


class ResponseModeRouter:
    def route(self, text: str) -> ResponseMode:
        if "还记得" in text or "记得" in text:
            return "memory_recall"
        if "好累" in text or "不想写代码" in text:
            return "comfort"
        if "爱弥斯" in text and ("原作" in text or "剧情" in text):
            return "lore_answer"
        if "代码" in text or "接口" in text or "架构" in text:
            return "technical_help"
        if "项目" in text or "计划" in text:
            return "planning"
        return "casual_chat"

