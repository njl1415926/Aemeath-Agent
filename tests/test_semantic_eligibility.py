from __future__ import annotations

import pytest

from app.semantic.eligibility import DeterministicSemanticEligibilityPolicyV01


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("早哇", "low_information_greeting"),
        ("HELLO!", "low_information_greeting"),
        ("好的。", "low_information_acknowledgement"),
        ("👍", "low_information_symbol_only"),
    ],
)
def test_exact_low_information_inputs_skip(text: str, reason: str):
    decision = DeterministicSemanticEligibilityPolicyV01().evaluate(
        text=text,
        depends_on_context=False,
    )
    assert not decision.eligible
    assert decision.reason_code == reason
    assert decision.version == "semantic-eligibility/v1"


@pytest.mark.parametrize(
    "text",
    [
        "还记得我之前说的吗",
        "上次那件事呢",
        "这个为什么会这样",
        "黑潮？",
        "第七章",
        "爱弥斯呢",
    ],
)
def test_recall_questions_and_short_content_words_remain_eligible(text: str):
    decision = DeterministicSemanticEligibilityPolicyV01().evaluate(
        text=text,
        depends_on_context=False,
    )
    assert decision.eligible


def test_context_dependency_overrides_exact_acknowledgement():
    decision = DeterministicSemanticEligibilityPolicyV01().evaluate(
        text="好",
        depends_on_context=True,
    )
    assert decision.eligible
    assert decision.reason_code == "context_dependent"
