from __future__ import annotations

from typing import Protocol

from app.quota.schemas import (
    StreamQuotaComplete,
    StreamQuotaFail,
    StreamQuotaProviderStarted,
    StreamQuotaRecoveryResult,
    StreamQuotaReservationRecord,
    StreamQuotaResolveOrReserve,
    StreamQuotaResolveOutcome,
    StreamQuotaTerminalRecord,
    UsageDailySummary,
    UsageOutputEstimate,
    UsageOutputEstimateRequest,
    UsagePromptEstimate,
    UsagePromptEstimateRequest,
    UsageQueryRequest,
    UsageQuotaPlan,
    UsageQuotaPlanRequest,
)


class UsageQuotaPolicyPort(Protocol):
    def plan_stream_reservation(self, *, request: UsageQuotaPlanRequest) -> UsageQuotaPlan:
        ...

    def estimate_prompt(self, *, request: UsagePromptEstimateRequest) -> UsagePromptEstimate:
        ...

    def estimate_output(self, *, request: UsageOutputEstimateRequest) -> UsageOutputEstimate:
        ...


class UsageQuotaQueryPort(Protocol):
    def get_daily_usage(self, *, request: UsageQueryRequest) -> UsageDailySummary | None:
        ...


class StreamQuotaTurnPersistencePort(Protocol):
    def resolve_or_reserve_with_quota(
        self,
        *,
        request: StreamQuotaResolveOrReserve,
    ) -> StreamQuotaResolveOutcome:
        ...

    def mark_provider_started_with_quota(
        self,
        *,
        request: StreamQuotaProviderStarted,
    ) -> StreamQuotaReservationRecord:
        ...

    def complete_with_quota(
        self,
        *,
        request: StreamQuotaComplete,
    ) -> StreamQuotaTerminalRecord:
        ...

    def fail_with_quota(
        self,
        *,
        request: StreamQuotaFail,
    ) -> StreamQuotaTerminalRecord:
        ...

    def recover_orphaned_with_quota(
        self,
        *,
        owner_runtime_id: str,
        limit: int = 100,
    ) -> StreamQuotaRecoveryResult:
        ...
