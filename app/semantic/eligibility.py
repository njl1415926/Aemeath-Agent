from __future__ import annotations

import unicodedata
from dataclasses import dataclass

SEMANTIC_ELIGIBILITY_VERSION = "semantic-eligibility/v1"

_GREETINGS = {
    "嗨",
    "hi",
    "hello",
    "你好",
    "您好",
    "早",
    "早安",
    "早上好",
    "早哇",
    "晚上好",
    "晚安",
}
_ACKNOWLEDGEMENTS = {
    "嗯",
    "嗯嗯",
    "好",
    "好的",
    "好呢",
    "行",
    "可以",
    "收到",
    "知道了",
    "明白",
    "对",
    "是的",
    "ok",
    "okay",
    "thanks",
    "谢谢",
    "谢啦",
}
_EXPLICIT_RECALL_MARKERS = (
    "还记得",
    "记得我",
    "上次",
    "之前说",
    "以前说",
    "回忆",
    "remember",
    "last time",
)
_TERMINAL_PUNCTUATION = "。！!，,、~～…"


@dataclass(frozen=True)
class SemanticEligibilityDecision:
    eligible: bool
    reason_code: str
    version: str = SEMANTIC_ELIGIBILITY_VERSION


class DeterministicSemanticEligibilityPolicyV01:
    version = SEMANTIC_ELIGIBILITY_VERSION

    def __init__(self, *, enabled: bool = True):
        self.enabled = enabled

    def evaluate(self, *, text: str, depends_on_context: bool) -> SemanticEligibilityDecision:
        if not self.enabled:
            return SemanticEligibilityDecision(True, "semantic_skip_disabled")
        normalized = text.strip()
        lowered = normalized.casefold()
        if any(marker in lowered for marker in _EXPLICIT_RECALL_MARKERS):
            return SemanticEligibilityDecision(True, "explicit_recall")
        if depends_on_context:
            return SemanticEligibilityDecision(True, "context_dependent")
        if "?" in normalized or "？" in normalized:
            return SemanticEligibilityDecision(True, "question_input")
        exact = normalized.rstrip(_TERMINAL_PUNCTUATION).strip().casefold()
        if exact in _GREETINGS:
            return SemanticEligibilityDecision(False, "low_information_greeting")
        if exact in _ACKNOWLEDGEMENTS:
            return SemanticEligibilityDecision(False, "low_information_acknowledgement")
        if normalized and all(
            char.isspace() or unicodedata.category(char)[0] in {"P", "S"}
            for char in normalized
        ):
            return SemanticEligibilityDecision(False, "low_information_symbol_only")
        return SemanticEligibilityDecision(True, "semantic_input_eligible")
