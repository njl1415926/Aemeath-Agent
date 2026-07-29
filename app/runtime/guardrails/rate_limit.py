from __future__ import annotations

import math
from collections import OrderedDict
from dataclasses import dataclass
from threading import Lock
from time import monotonic
from typing import Callable

from app.runtime.guardrails.schemas import ApiRateLimitDecision, ApiRateLimitRequest


@dataclass
class _Bucket:
    tokens: float
    updated_at: float
    last_used_at: float


class InProcessApiRateLimiter:
    def __init__(
        self,
        *,
        user_rate_per_minute: int,
        user_burst: int,
        ip_rate_per_minute: int,
        ip_burst: int,
        idle_ttl_seconds: float,
        sweep_interval_seconds: float = 30.0,
        sweep_batch_size: int = 128,
        clock: Callable[[], float] = monotonic,
    ):
        values = (user_rate_per_minute, user_burst, ip_rate_per_minute, ip_burst)
        if (
            min(values) <= 0
            or not math.isfinite(idle_ttl_seconds)
            or idle_ttl_seconds <= 0
            or not math.isfinite(sweep_interval_seconds)
            or sweep_interval_seconds <= 0
            or isinstance(sweep_batch_size, bool)
            or sweep_batch_size <= 0
        ):
            raise ValueError("invalid API rate limit configuration")
        self._user_rate = user_rate_per_minute / 60.0
        self._user_burst = float(user_burst)
        self._ip_rate = ip_rate_per_minute / 60.0
        self._ip_burst = float(ip_burst)
        self._idle_ttl_seconds = idle_ttl_seconds
        self._sweep_interval_seconds = sweep_interval_seconds
        self._sweep_batch_size = sweep_batch_size
        self._clock = clock
        self._lock = Lock()
        self._buckets: OrderedDict[tuple[str, str], _Bucket] = OrderedDict()
        self._next_sweep_at: float | None = None

    def check(self, *, request: ApiRateLimitRequest) -> ApiRateLimitDecision:
        now = self._clock()
        with self._lock:
            self._maybe_sweep(now)
            user_wait = self._peek_wait(
                key=("user", request.user_id), rate=self._user_rate,
                burst=self._user_burst, now=now,
            )
            ip_wait = 0.0
            if request.ip_key:
                ip_wait = self._peek_wait(
                    key=("ip", request.ip_key), rate=self._ip_rate,
                    burst=self._ip_burst, now=now,
                )
            retry_wait = max(user_wait, ip_wait)
            if retry_wait > 0:
                return ApiRateLimitDecision(
                    allowed=False,
                    reason_code=(
                        "user_rate_limit" if user_wait > 0 else "ip_rate_limit"
                    ),
                    retry_after_seconds=max(1, math.ceil(retry_wait)),
                )
            self._debit(("user", request.user_id), now)
            if request.ip_key:
                self._debit(("ip", request.ip_key), now)
            return ApiRateLimitDecision(True, "rate_limit_allowed")

    def _peek_wait(
        self, *, key: tuple[str, str], rate: float, burst: float, now: float
    ) -> float:
        bucket = self._buckets.get(key)
        if bucket is None:
            self._buckets[key] = _Bucket(burst, now, now)
            return 0.0
        elapsed = max(0.0, now - bucket.updated_at)
        bucket.tokens = min(burst, bucket.tokens + elapsed * rate)
        bucket.updated_at = now
        bucket.last_used_at = now
        self._buckets.move_to_end(key)
        return 0.0 if bucket.tokens >= 1.0 else (1.0 - bucket.tokens) / rate

    def _debit(self, key: tuple[str, str], now: float) -> None:
        bucket = self._buckets[key]
        bucket.tokens -= 1.0
        bucket.last_used_at = now

    def _maybe_sweep(self, now: float) -> None:
        if self._next_sweep_at is None:
            self._next_sweep_at = now + self._sweep_interval_seconds
            return
        if now < self._next_sweep_at:
            return
        stale_backlog = self._sweep(now)
        self._next_sweep_at = now if stale_backlog else now + self._sweep_interval_seconds

    def _sweep(self, now: float) -> bool:
        removed = 0
        while self._buckets and removed < self._sweep_batch_size:
            _, oldest = next(iter(self._buckets.items()))
            if now - oldest.last_used_at < self._idle_ttl_seconds:
                break
            self._buckets.popitem(last=False)
            removed += 1
        if removed < self._sweep_batch_size or not self._buckets:
            return False
        oldest = next(iter(self._buckets.values()))
        return now - oldest.last_used_at >= self._idle_ttl_seconds
