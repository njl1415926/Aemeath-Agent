from __future__ import annotations

import argparse
import json
import os
import statistics
from dataclasses import asdict
from time import perf_counter

from app.auth.passwords import APPROVED_PASSWORD_PROFILE, PasswordEngine


def qualify_password_profile(
    *,
    samples: int = 7,
    p95_limit_seconds: float = 1.0,
    require_single_cpu: bool = False,
) -> dict:
    if samples < 3:
        raise ValueError("samples_must_be_at_least_three")
    engine = PasswordEngine()
    material = "fixed qualification material 2026"
    warmup = engine.hash_password(material)
    if not engine.verify_password(warmup, material):
        raise RuntimeError("password_qualification_warmup_failed")
    hash_durations: list[float] = []
    verify_durations: list[float] = []
    for _ in range(samples):
        started = perf_counter()
        encoded = engine.hash_password(material)
        hash_durations.append(perf_counter() - started)
        started = perf_counter()
        if not engine.verify_password(encoded, material):
            raise RuntimeError("password_qualification_verify_failed")
        verify_durations.append(perf_counter() - started)
    hash_stats = _duration_stats(hash_durations)
    verify_stats = _duration_stats(verify_durations)
    p95 = max(hash_stats["p95_seconds"], verify_stats["p95_seconds"])
    cpu_affinity_count = _cpu_affinity_count()
    single_cpu_passed = not require_single_cpu or cpu_affinity_count == 1
    return {
        "profile": asdict(APPROVED_PASSWORD_PROFILE),
        "samples": samples,
        "concurrency": 1,
        "cpu_affinity_count": cpu_affinity_count,
        "single_cpu_required": require_single_cpu,
        "hash": hash_stats,
        "verify": verify_stats,
        "peak_rss_bytes": _peak_rss_bytes(),
        "p95_limit_seconds": p95_limit_seconds,
        "passed": p95 < p95_limit_seconds and single_cpu_passed,
    }


def _duration_stats(durations: list[float]) -> dict[str, float]:
    ordered = sorted(durations)
    p95_index = min(len(ordered) - 1, max(0, round(0.95 * len(ordered) + 0.5) - 1))
    return {
        "p50_seconds": round(statistics.median(ordered), 6),
        "p95_seconds": round(ordered[p95_index], 6),
        "p99_seconds": round(ordered[-1], 6),
    }


def _cpu_affinity_count() -> int | None:
    get_affinity = getattr(os, "sched_getaffinity", None)
    if get_affinity is None:
        return None
    return len(get_affinity(0))


def _peak_rss_bytes() -> int | None:
    try:
        import resource
    except ImportError:
        return None
    peak = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    if os.uname().sysname == "Darwin":
        return peak
    return peak * 1024


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Qualify the frozen Argon2id password profile.")
    parser.add_argument("--samples", type=int, default=7)
    parser.add_argument("--require-single-cpu", action="store_true")
    args = parser.parse_args(argv)
    result = qualify_password_profile(
        samples=args.samples,
        require_single_cpu=args.require_single_cpu,
    )
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
