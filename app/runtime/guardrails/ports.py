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


class DialogueAdmissionHandle(Protocol):
    @property
    def lease(self) -> DialogueAdmissionLease: ...

    def close(self) -> None: ...


class DialogueAdmissionPort(Protocol):
    def open_handle(
        self, *, request: DialogueAdmissionRequest
    ) -> DialogueAdmissionHandle: ...

    def acquire(
        self, *, request: DialogueAdmissionRequest
    ) -> AbstractContextManager[DialogueAdmissionLease]: ...


class ApiRateLimiterPort(Protocol):
    def check(self, *, request: ApiRateLimitRequest) -> ApiRateLimitDecision: ...


class ChatInputLimitPolicyPort(Protocol):
    def validate(
        self, *, request: ChatInputLimitRequest
    ) -> ChatInputLimitDecision: ...
