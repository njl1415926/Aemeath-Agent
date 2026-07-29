from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Protocol

from app.runtime.guardrails.schemas import (
    ApiRateLimitDecision,
    ApiRateLimitRequest,
    ChatInputLimitDecision,
    ChatInputLimitRequest,
    DialogueAdmissionLease,
    DialogueAdmissionRequest,
)


class DialogueAdmissionPort(Protocol):
    def acquire(
        self, *, request: DialogueAdmissionRequest
    ) -> AbstractContextManager[DialogueAdmissionLease]: ...


class ApiRateLimiterPort(Protocol):
    def check(self, *, request: ApiRateLimitRequest) -> ApiRateLimitDecision: ...


class ChatInputLimitPolicyPort(Protocol):
    def validate(
        self, *, request: ChatInputLimitRequest
    ) -> ChatInputLimitDecision: ...
