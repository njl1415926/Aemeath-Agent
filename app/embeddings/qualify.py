from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

from dotenv import load_dotenv

from app.embeddings.config import (
    load_baseline,
    load_canary_set,
    load_profile_for_qualification,
)
from app.embeddings.errors import EmbeddingQualificationError
from app.embeddings.factory import create_qualification_provider
from app.embeddings.observability import record_qualification_terminal
from app.embeddings.qualification import (
    capture_candidate,
    probe_report_to_dict,
    run_qualification_probe,
)
from app.embeddings.schemas import EmbeddingRuntimeOptions, QualificationThresholds
from app.errors import map_exception
from app.errors.codes import ErrorCode
from app.observability.app_logger import JsonlAppLogger
from app.observability.noop import NoopAppLogger
from app.observability.ports import AppLoggerPort


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Capture or verify an EmbeddingSpaceProfile qualification artifact."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    capture = subparsers.add_parser(
        "capture-candidate",
        help="Capture a review profile into a new candidate directory.",
    )
    _add_common_arguments(capture)
    capture.add_argument(
        "--output-dir",
        type=Path,
        help=(
            "New candidate directory. Defaults below "
            ".aemeath/embedding_qualification_candidates/."
        ),
    )
    capture.add_argument("--min-cosine-per-vector", type=float, default=0.99999)
    capture.add_argument("--max-abs-coordinate-delta", type=float, default=0.0001)

    verify = subparsers.add_parser(
        "verify",
        help="Verify a profile that already references a committed baseline.",
    )
    _add_common_arguments(verify)
    return parser


def _add_common_arguments(parser: argparse.ArgumentParser) -> None:
    defaults = EmbeddingRuntimeOptions()
    parser.add_argument("--profile", required=True)
    parser.add_argument("--repeat-count", type=int, default=2)
    parser.add_argument("--batch-size", type=int, default=defaults.batch_size)
    parser.add_argument("--timeout-seconds", type=float, default=defaults.timeout_seconds)
    parser.add_argument("--max-retries", type=int, default=defaults.max_retries)
    parser.add_argument(
        "--retry-backoff-seconds",
        type=float,
        default=defaults.retry_backoff_seconds,
    )
    parser.add_argument(
        "--local-model-path",
        type=Path,
        default=defaults.local_model_path,
    )
    parser.add_argument("--local-device", default=defaults.local_device)
    parser.add_argument("--local-max-length", type=int, default=defaults.local_max_length)
    parser.add_argument(
        "--allow-hash",
        action="store_true",
        help="Explicitly allow the deterministic test/development provider.",
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    app_logger: AppLoggerPort | None = None,
    clock: Callable[[], float] = time.perf_counter,
) -> int:
    # Explicit maintainer CLI invocations follow the project's normal .env loading
    # convention.  The resolved secret is never copied into arguments or output.
    load_dotenv()
    args = build_parser().parse_args(argv)
    logger = (
        app_logger if app_logger is not None else _build_qualification_app_logger()
    )
    started_at = clock()
    profile = None
    canary_set = None
    probe = None
    try:
        profile = load_profile_for_qualification(args.profile)
        canary_set = load_canary_set(profile)
        runtime = EmbeddingRuntimeOptions(
            batch_size=args.batch_size,
            timeout_seconds=args.timeout_seconds,
            max_retries=args.max_retries,
            retry_backoff_seconds=args.retry_backoff_seconds,
            local_model_path=args.local_model_path,
            local_device=args.local_device,
            local_max_length=args.local_max_length,
            allow_hash=args.allow_hash,
        )

        if args.command == "capture-candidate":
            if profile.release_status != "review":
                raise EmbeddingQualificationError("capture_requires_review_profile")
            output_dir = args.output_dir or _default_candidate_dir(profile.profile_id)
            # Refuse the path before loading a model or making a paid API call.
            if output_dir.exists():
                raise EmbeddingQualificationError("candidate_output_already_exists")
            provider = create_qualification_provider(
                profile,
                runtime,
                app_logger=logger,
            )
            captured = capture_candidate(
                profile=profile,
                canary_set=canary_set,
                provider=provider,
                output_dir=output_dir,
                thresholds=QualificationThresholds(
                    min_cosine_per_vector=args.min_cosine_per_vector,
                    max_abs_coordinate_delta=args.max_abs_coordinate_delta,
                ),
                repeat_count=args.repeat_count,
            )
            probe = captured.probe
            error_code = (
                None
                if probe.passed
                else ErrorCode.EMBEDDING_QUALIFICATION_FAILED.value
            )
            _record_terminal(
                logger,
                args=args,
                profile=profile,
                canary_set=canary_set,
                probe=probe,
                started_at=started_at,
                clock=clock,
                error_code=error_code,
            )
            _print_json(
                {
                    "passed": probe.passed,
                    "command": args.command,
                    "profile_id": profile.profile_id,
                    "output_dir": str(captured.output_dir),
                    "baseline_artifact_sha256": captured.baseline_artifact_sha256,
                    "expected_fingerprint": captured.expected_fingerprint,
                    "error_code": error_code,
                }
            )
            return 0 if probe.passed else 1

        # Both review and approved manifests may be verified once all baseline fields
        # are filled.  Only the normal runtime Factory restricts selection to approved.
        baseline = load_baseline(profile, canary_count=len(canary_set.items))
        provider = create_qualification_provider(
            profile,
            runtime,
            app_logger=logger,
        )
        probe = run_qualification_probe(
            profile=profile,
            canary_set=canary_set,
            baseline=baseline,
            provider=provider,
            repeat_count=args.repeat_count,
        )
        error_code = (
            None if probe.passed else ErrorCode.EMBEDDING_QUALIFICATION_FAILED.value
        )
        _record_terminal(
            logger,
            args=args,
            profile=profile,
            canary_set=canary_set,
            probe=probe,
            started_at=started_at,
            clock=clock,
            error_code=error_code,
        )
        payload = probe_report_to_dict(probe)
        payload.update(
            {
                "schema_version": "embedding-qualification-report/v1",
                "command": args.command,
                "error_code": error_code,
            }
        )
        _print_json(payload)
        return 0 if probe.passed else 1
    except Exception as exc:
        error = map_exception(exc)
        _record_terminal(
            logger,
            args=args,
            profile=profile,
            canary_set=canary_set,
            probe=probe,
            started_at=started_at,
            clock=clock,
            error_code=error.code.value,
        )
        _print_json(
            {
                "passed": False,
                "command": getattr(args, "command", None),
                "profile_id": _safe_profile_id(getattr(args, "profile", None)),
                "error_code": error.code.value,
                "retryable": error.retryable,
            }
        )
        return 2


def _record_terminal(
    app_logger: AppLoggerPort,
    *,
    args: argparse.Namespace,
    profile,
    canary_set,
    probe,
    started_at: float,
    clock: Callable[[], float],
    error_code: str | None,
) -> None:
    passed = probe is not None and probe.passed and error_code is None
    record_qualification_terminal(
        app_logger,
        passed=passed,
        command=args.command,
        provider=profile.provider if profile is not None else None,
        profile_id=(
            profile.profile_id
            if profile is not None
            else _safe_profile_id(getattr(args, "profile", None))
        ),
        release_status=profile.release_status if profile is not None else None,
        dimension=profile.dimension if profile is not None else None,
        canary_count=len(canary_set.items) if canary_set is not None else None,
        observation_count=len(probe.observations) if probe is not None else 0,
        signature_fingerprint=(
            probe.signature_fingerprint if probe is not None else None
        ),
        duration_ms=max(0.0, (clock() - started_at) * 1000.0),
        error_code=error_code,
    )


def _build_qualification_app_logger() -> AppLoggerPort:
    enabled = os.getenv("AEMEATH_ENABLE_APP_LOG", "false").strip().lower()
    if enabled not in {"1", "true", "yes", "on"}:
        return NoopAppLogger()
    root = Path(os.getenv("AEMEATH_APP_LOG_ROOT", ".aemeath/app_logs"))
    return JsonlAppLogger(root_dir=root)


def _default_candidate_dir(profile_id: str) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    suffix = secrets.token_hex(4)
    return (
        Path(".aemeath")
        / "embedding_qualification_candidates"
        / f"{profile_id}-{timestamp}-{suffix}"
    )


def _safe_profile_id(value: object) -> str | None:
    if isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value):
        return value
    return None


def _print_json(payload: dict[str, object]) -> None:
    # The allowlisted payloads never include text, vectors, credentials, headers, or
    # response bodies.  Keeping this helper compact also avoids accidental repr output.
    sys.stdout.write(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n"
    )


if __name__ == "__main__":
    raise SystemExit(main())
