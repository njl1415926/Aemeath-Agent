from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

from app.quota.schemas import (
    ESTIMATOR_VERSION_PROMPT_UTF8_V0_1,
    UsageOutputEstimate,
    UsageOutputEstimateRequest,
    UsagePromptEstimate,
    UsagePromptEstimateRequest,
    UsageQuotaPlan,
    UsageQuotaPlanRequest,
)


class PromptUtf8UsageQuotaPolicy:
    estimator_version = ESTIMATOR_VERSION_PROMPT_UTF8_V0_1

    def plan_stream_reservation(self, *, request: UsageQuotaPlanRequest) -> UsageQuotaPlan:
        self._require_supported(request.estimator_version)
        input_ceiling = request.context_budget_max_chars * 4
        return UsageQuotaPlan(
            user_id=request.user_id,
            quota_day=request.quota_day,
            surface=request.surface,
            character_id=request.character_id,
            turn_limit=request.turn_limit,
            token_limit=request.token_limit,
            estimator_version=request.estimator_version,
            context_budget_max_input_utf8_ceiling=input_ceiling,
            reserved_input_tokens=_ceil_div(input_ceiling, 3),
            reserved_output_tokens=request.max_output_tokens,
        )

    def estimate_prompt(self, *, request: UsagePromptEstimateRequest) -> UsagePromptEstimate:
        self._require_supported(request.estimator_version)
        return UsagePromptEstimate(
            estimator_version=request.estimator_version,
            prompt_messages_utf8_bytes=request.prompt_messages_utf8_bytes,
            prompt_input_tokens=_ceil_div(request.prompt_messages_utf8_bytes, 3),
            reserved_output_tokens=request.max_output_tokens,
        )

    def estimate_output(self, *, request: UsageOutputEstimateRequest) -> UsageOutputEstimate:
        self._require_supported(request.estimator_version)
        return UsageOutputEstimate(
            estimator_version=request.estimator_version,
            response_utf8_bytes=request.response_utf8_bytes,
            output_tokens=min(
                request.max_output_tokens,
                _ceil_div(request.response_utf8_bytes, 3),
            ),
            delta_count=request.delta_count,
        )

    def _require_supported(self, estimator_version: str) -> None:
        if estimator_version != self.estimator_version:
            raise ValueError("quota_estimator_version_unsupported")


def prompt_messages_utf8_bytes(prompt_messages: Sequence[Mapping[str, object]]) -> int:
    """Return a deterministic count for complete prepared prompt messages."""

    canonical = json.dumps(
        list(prompt_messages),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return len(canonical.encode("utf-8"))


def _ceil_div(value: int, divisor: int) -> int:
    return (value + divisor - 1) // divisor
