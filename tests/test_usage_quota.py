from __future__ import annotations

from app.quota.schemas import (
    ESTIMATOR_VERSION_PROMPT_UTF8_V0_1,
    UsageOutputEstimateRequest,
    UsagePromptEstimateRequest,
    UsageQuotaPlanRequest,
)
from app.quota.service import PromptUtf8UsageQuotaPolicy, prompt_messages_utf8_bytes


def test_prompt_utf8_policy_reserves_context_budget_ceiling():
    policy = PromptUtf8UsageQuotaPolicy()

    plan = policy.plan_stream_reservation(
        request=UsageQuotaPlanRequest(
            user_id="user_1",
            quota_day="2026-08-10",
            surface="stream_chat",
            character_id="aemeath",
            turn_limit=50,
            token_limit=1000,
            estimator_version=ESTIMATOR_VERSION_PROMPT_UTF8_V0_1,
            context_budget_max_chars=99,
            max_output_tokens=32,
        )
    )

    assert plan.context_budget_max_input_utf8_ceiling == 396
    assert plan.reserved_input_tokens == 132
    assert plan.reserved_output_tokens == 32
    assert plan.reserved_tokens == 164


def test_prompt_utf8_policy_counts_complete_prompt_messages_without_ascii_escape():
    messages = [
        {"role": "system", "content": "Aemeath"},
        {"role": "user", "content": "中文🙂"},
    ]
    expected = len(
        (
            '[{"content":"Aemeath","role":"system"},'
            '{"content":"中文🙂","role":"user"}]'
        ).encode("utf-8")
    )

    assert prompt_messages_utf8_bytes(messages) == expected


def test_prompt_and_output_estimates_are_deterministic_and_bounded():
    policy = PromptUtf8UsageQuotaPolicy()

    prompt = policy.estimate_prompt(
        request=UsagePromptEstimateRequest(
            estimator_version=ESTIMATOR_VERSION_PROMPT_UTF8_V0_1,
            prompt_messages_utf8_bytes=10,
            max_output_tokens=7,
        )
    )
    output = policy.estimate_output(
        request=UsageOutputEstimateRequest(
            estimator_version=ESTIMATOR_VERSION_PROMPT_UTF8_V0_1,
            response_utf8_bytes=100,
            delta_count=3,
            max_output_tokens=7,
        )
    )

    assert prompt.prompt_input_tokens == 4
    assert prompt.reserved_tokens == 11
    assert output.output_tokens == 7
    assert output.delta_count == 3
