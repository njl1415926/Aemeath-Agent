from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from app.deployment.lock_contract import validate_deployment_lock  # noqa: E402


def main() -> int:
    report = validate_deployment_lock(
        pyproject_text=(PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"),
        lock_text=(PROJECT_ROOT / "requirements.deploy.lock").read_text(encoding="utf-8"),
    )
    payload = {
        "status": "ok" if report.valid else "failed",
        "missing_packages": list(report.missing_packages),
        "constraint_mismatches": list(report.constraint_mismatches),
        "unpinned_packages": list(report.unpinned_packages),
        "unhashed_packages": list(report.unhashed_packages),
    }
    print(json.dumps(payload, sort_keys=True))
    return 0 if report.valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
