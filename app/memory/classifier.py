from __future__ import annotations

from app.schemas import MemoryDecision

HARD_MEMORY_TRIGGERS = [
    "记住",
    "请记住",
    "帮我记一下",
    "你要记得",
    "以后要",
    "以后别忘了",
    "下次记得",
]


class MemoryWriteClassifier:
    def decide(self, *, user_input: str, assistant_response: str) -> MemoryDecision:
        text = user_input.strip()
        if any(trigger in text for trigger in HARD_MEMORY_TRIGGERS):
            return MemoryDecision(
                should_write=True,
                operation="create",
                namespace="user_profile",
                memory_type="llm_extraction_job",
                content=text,
                importance=9,
                confidence=0.98,
                source="explicit_user_statement",
                reason="用户显式要求长期记忆提炼。",
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
