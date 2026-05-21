from __future__ import annotations

from app.schemas import StateUpdate


class StateUpdateClassifier:
    def decide(self, *, user_input: str, assistant_response: str) -> StateUpdate:
        text = user_input.strip()
        if "好累" in text or "不想写代码" in text:
            return StateUpdate(
                user_emotion="tired",
                aemeath_mood="concerned",
                trust_delta=0,
                affection_delta=1,
                familiarity_delta=0,
                last_topic="wellbeing",
                reason="用户表达疲惫。",
            )
        if "记住" in text:
            return StateUpdate(
                user_emotion="focused",
                aemeath_mood="attentive",
                trust_delta=1,
                affection_delta=0,
                familiarity_delta=0,
                last_topic="memory_instruction",
                reason="用户给出明确指令。",
            )
        if "项目" in text or "计划" in text:
            return StateUpdate(
                user_emotion="motivated",
                aemeath_mood="focused",
                trust_delta=1,
                affection_delta=0,
                familiarity_delta=0,
                last_topic="agent_project",
                reason="用户在认真讨论项目。",
            )
        if "哈哈" in text or "开玩笑" in text:
            return StateUpdate(
                user_emotion="light",
                aemeath_mood="playful",
                trust_delta=0,
                affection_delta=0,
                familiarity_delta=1,
                last_topic="casual_chat",
                reason="用户语气轻松。",
            )
        return StateUpdate(
            user_emotion="neutral",
            aemeath_mood="calm",
            trust_delta=0,
            affection_delta=0,
            familiarity_delta=0,
            last_topic="general",
            reason="默认状态更新。",
        )

