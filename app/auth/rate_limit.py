from __future__ import annotations

import hashlib
import hmac
import math
import secrets
from collections import OrderedDict
from dataclasses import dataclass
from threading import Lock
from time import monotonic
from typing import Callable


@dataclass
class _Bucket:
    tokens: float
    updated_at: float
    last_used_at: float


@dataclass(frozen=True)
class PreAuthRateDecision:
    allowed: bool
    retry_after_seconds: int = 0
    reason: str = "allowed"


class InProcessPreAuthRateLimiter:
    def __init__(
        self,
        *,
        rate_per_minute: int = 20,
        burst: int = 5,
        global_rate_per_minute: int = 300,
        global_burst: int = 50,
        idle_ttl_seconds: float = 600.0,
        max_buckets: int = 4096,
        sweep_batch_size: int = 128,
        clock: Callable[[], float] = monotonic,
        pseudonym_key: bytes | None = None,
    ):
        values = (
            rate_per_minute,
            burst,
            global_rate_per_minute,
            global_burst,
            max_buckets,
            sweep_batch_size,
        )
        if min(values) <= 0 or idle_ttl_seconds <= 0:
            raise ValueError("invalid_pre_auth_rate_limit_configuration")
        self._rate = rate_per_minute / 60.0
        self._burst = float(burst)
        self._global_rate = global_rate_per_minute / 60.0
        self._global_burst = float(global_burst)
        self._idle_ttl = idle_ttl_seconds
        self._max_buckets = max_buckets
        self._sweep_batch_size = sweep_batch_size
        self._clock = clock
        self._key = pseudonym_key or secrets.token_bytes(32)
        self._lock = Lock()
        self._buckets: OrderedDict[tuple[str, str], _Bucket] = OrderedDict()

    def check(
        self,
        *,
        endpoint: str,
        ip_value: str | None,
        identity_value: str | None = None,
    ) -> PreAuthRateDecision:
        keys: list[tuple[tuple[str, str], float, float, str]] = [
            (("global", self._pseudo(endpoint)), self._global_rate, self._global_burst, "global")
        ]
        if ip_value:
            keys.append((("ip", self._pseudo(ip_value)), self._rate, self._burst, "ip"))
        if identity_value:
            keys.append(
                (
                    ("identity", self._pseudo(identity_value)),
                    self._rate,
                    self._burst,
                    "identity",
                )
            )
        return self._check_keys(keys)

    def check_request(
        self,
        *,
        endpoint: str,
        ip_value: str | None,
    ) -> PreAuthRateDecision:
        keys: list[tuple[tuple[str, str], float, float, str]] = [
            (("global", self._pseudo(endpoint)), self._global_rate, self._global_burst, "global")
        ]
        if ip_value:
            keys.append((("ip", self._pseudo(ip_value)), self._rate, self._burst, "ip"))
        return self._check_keys(keys)

    def check_identity(self, *, identity_value: str) -> PreAuthRateDecision:
        return self._check_keys(
            [
                (
                    ("identity", self._pseudo(identity_value)),
                    self._rate,
                    self._burst,
                    "identity",
                )
            ]
        )

    def _check_keys(
        self,
        keys: list[tuple[tuple[str, str], float, float, str]],
    ) -> PreAuthRateDecision:
        now = self._clock()
        with self._lock:
            self._sweep(now)
            missing = sum(1 for key, *_ in keys if key not in self._buckets)
            if len(self._buckets) + missing > self._max_buckets:
                return PreAuthRateDecision(False, 1, "capacity")
            waits = [
                (self._peek_wait(key=key, rate=rate, burst=burst, now=now), reason)
                for key, rate, burst, reason in keys
            ]
            retry = max(wait for wait, _ in waits)
            if retry > 0:
                reason = next(reason for wait, reason in waits if wait == retry)
                return PreAuthRateDecision(False, max(1, math.ceil(retry)), reason)
            for key, *_ in keys:
                self._buckets[key].tokens -= 1.0
            return PreAuthRateDecision(True)

    def _pseudo(self, value: str) -> str:
        return hmac.new(self._key, value.encode("utf-8"), hashlib.sha256).hexdigest()

    def _peek_wait(self, *, key: tuple[str, str], rate: float, burst: float, now: float) -> float:
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = _Bucket(burst, now, now)
            self._buckets[key] = bucket
        elapsed = max(0.0, now - bucket.updated_at)
        bucket.tokens = min(burst, bucket.tokens + elapsed * rate)
        bucket.updated_at = now
        bucket.last_used_at = now
        self._buckets.move_to_end(key)
        if bucket.tokens >= 1.0:
            return 0.0
        return (1.0 - bucket.tokens) / rate

    def _sweep(self, now: float) -> None:
        removed = 0
        while self._buckets and removed < self._sweep_batch_size:
            key, bucket = next(iter(self._buckets.items()))
            if now - bucket.last_used_at < self._idle_ttl:
                break
            del self._buckets[key]
            removed += 1
