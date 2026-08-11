from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.deployment.log_retention import (  # noqa: E402
    LogRetentionPolicy,
    apply_jsonl_retention,
    plan_jsonl_retention,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Safely prune Aemeath JSONL logs.")
    parser.add_argument("--kind", choices=("app", "trace"), required=True)
    parser.add_argument("--log-root", type=Path, default=Path("/var/log/aemeath"))
    parser.add_argument("--retention-days", type=int, required=True)
    parser.add_argument("--max-total-bytes", type=int, required=True)
    parser.add_argument("--active-grace-seconds", type=int, default=3600)
    parser.add_argument("--apply", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    policy = LogRetentionPolicy(
        retention_days=args.retention_days,
        max_total_bytes=args.max_total_bytes,
        active_grace_seconds=args.active_grace_seconds,
    )
    try:
        base = args.log_root.resolve(strict=True)
        root = base / args.kind
        plan = plan_jsonl_retention(
            root=root,
            policy=policy,
            now=datetime.now(timezone.utc),
        )
        if args.apply:
            result = apply_jsonl_retention(
                plan=plan,
                policy=policy,
                now=datetime.now(timezone.utc),
            )
            payload = {
                "status": "ok" if result.target_satisfied else "retention_target_unmet",
                "mode": "apply",
                "kind": args.kind,
                "planned_file_count": result.planned_file_count,
                "deleted_file_count": result.deleted_file_count,
                "skipped_changed_file_count": result.skipped_changed_file_count,
                "deleted_bytes": result.deleted_bytes,
                "remaining_candidate_count": result.remaining_candidate_count,
                "total_bytes_after": result.total_bytes_after,
            }
            print(json.dumps(payload, sort_keys=True))
            return 0 if result.target_satisfied else 3
        payload = {
            "status": "ok" if plan.target_satisfied else "retention_target_unmet",
            "mode": "dry_run",
            "kind": args.kind,
            "scanned_file_count": plan.scanned_file_count,
            "planned_file_count": len(plan.entries),
            "skipped_symlink_count": plan.skipped_symlink_count,
            "total_bytes_before": plan.total_bytes_before,
            "projected_bytes_after": plan.projected_bytes_after,
        }
        print(json.dumps(payload, sort_keys=True))
        return 0 if plan.target_satisfied else 3
    except (OSError, ValueError):
        print(
            json.dumps(
                {"status": "error", "mode": "apply" if args.apply else "dry_run"},
                sort_keys=True,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
