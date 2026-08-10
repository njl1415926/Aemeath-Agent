from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.deployment.sqlite_backup import (  # noqa: E402
    BackupCommittedDurabilityUncertain,
    D6SqliteError,
    create_sqlite_backup,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create an online SQLite backup.")
    parser.add_argument("--db-path", required=True, type=Path)
    parser.add_argument("--backup-root", required=True, type=Path)
    parser.add_argument("--backend", required=True)
    parser.add_argument("--label", default="manual")
    parser.add_argument("--busy-timeout-ms", type=int, default=5000)
    parser.add_argument("--pages-per-step", type=int, default=256)
    parser.add_argument("--sleep-ms", type=int, default=50)
    parser.add_argument("--max-duration-seconds", type=float, default=300.0)
    parser.add_argument(
        "--app-image-digest",
        default=os.getenv("AEMEATH_APP_IMAGE_DIGEST", "unknown"),
    )
    parser.add_argument(
        "--ops-image-digest",
        default=os.getenv("AEMEATH_OPS_IMAGE_DIGEST", "unknown"),
    )
    parser.add_argument(
        "--deploy-lock-sha256",
        default=os.getenv("AEMEATH_DEPLOY_LOCK_SHA256", "unknown"),
    )
    parser.add_argument(
        "--compose-config-sha256",
        default=os.getenv("AEMEATH_COMPOSE_CONFIG_SHA256", "unknown"),
    )
    parser.add_argument("--git-commit", default=os.getenv("AEMEATH_BUILD_COMMIT", "unknown"))
    parser.add_argument("--compose-project", default=os.getenv("COMPOSE_PROJECT_NAME", "aemeath"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = create_sqlite_backup(
            db_path=args.db_path,
            backup_root=args.backup_root,
            backend=args.backend,
            label=args.label,
            busy_timeout_ms=args.busy_timeout_ms,
            pages_per_step=args.pages_per_step,
            sleep_ms=args.sleep_ms,
            max_duration_seconds=args.max_duration_seconds,
            app_image_digest=args.app_image_digest,
            ops_image_digest=args.ops_image_digest,
            deploy_lock_sha256=args.deploy_lock_sha256,
            compose_config_sha256=args.compose_config_sha256,
            git_commit=args.git_commit,
            compose_project=args.compose_project,
        )
    except BackupCommittedDurabilityUncertain as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "reason": exc.reason,
                    "backup_id": exc.backup_id,
                    "backup_dir": str(exc.backup_dir),
                },
                sort_keys=True,
            )
        )
        return exc.exit_code
    except D6SqliteError as exc:
        print(json.dumps({"status": "failed", "reason": exc.reason}, sort_keys=True))
        return exc.exit_code
    except Exception:
        print(json.dumps({"status": "failed", "reason": "internal_error"}, sort_keys=True))
        return 70
    print(
        json.dumps(
            {
                "status": "ok",
                "backup_id": result.backup_id,
                "backup_dir": str(result.backup_dir),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
