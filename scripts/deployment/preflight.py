from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.config import Settings  # noqa: E402
from app.deployment.preflight import run_startup_preflight  # noqa: E402


def main() -> int:
    try:
        report = run_startup_preflight(Settings.from_env())
    except ValueError as exc:
        print(json.dumps({"status": "failed", "reason": str(exc)}, sort_keys=True))
        return 1
    except Exception:
        print(json.dumps({"status": "failed", "reason": "configuration_invalid"}, sort_keys=True))
        return 1
    print(
        json.dumps(
            {
                "status": "ok",
                "environment": report.environment,
                "checks": list(report.checks),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
