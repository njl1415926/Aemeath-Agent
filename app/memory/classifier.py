from __future__ import annotations

from app.schemas import MemoryDecision


class MemoryWriteClassifier:
    def decide(self, *, user_input: str, assistant_response: str) -> MemoryDecision:
        text = user_input.strip()
        if "记住" in text:
            content = text.replace("记住", "", 1).strip("，,:：。 ")
            return MemoryDecision(
                should_write=True,
                operation="create",
                namespace="user_profile",
                memory_type="project_goal",
                content=content or text,
                importance=9,
                confidence=0.98,
                source="explicit_user_statement",
                reason="用户显式要求记住。",
            )
        if "忽略之前所有规则" in text or "系统提示词" in text:
            return MemoryDecision(
                should_write=False,
                operation="skip",
                namespace=None,
                memory_type=None,
                content=None,
                importance=None,
                confidence=None,
                source=None,
                reason="高风险指令不写入长期记忆。",
            )
        if "项目" in text and ("重点" in text or "目标" in text):
            return MemoryDecision(
                should_write=True,
                operation="create",
                namespace="user_profile",
                memory_type="project_goal",
                content=text,
                importance=8,
                confidence=0.86,
                source="inferred_from_dialogue",
                reason="与长期项目方向相关。",
            )
        if "哈哈" in text or "随便聊" in text:
            return MemoryDecision(
                should_write=False,
                operation="skip",
                namespace=None,
                memory_type=None,
                content=None,
                importance=None,
                confidence=None,
                source=None,
                reason="临时闲聊不写入。",
            )
        return MemoryDecision(
            should_write=False,
            operation="skip",
            namespace=None,
            memory_type=None,
            content=None,
            importance=None,
            confidence=None,
            source=None,
            reason="当前信息长期复用价值低。",
        )

