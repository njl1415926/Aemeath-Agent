from __future__ import annotations

import json
import sys

_DISABLED_PROFILES = {
    "memory-worker": "memory_worker_gates_not_approved",
}


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    profile = arguments[0] if len(arguments) == 1 else "invalid"
    reason = _DISABLED_PROFILES.get(profile, "profile_not_allowed")
    print(json.dumps({"status": "disabled", "reason": reason}, sort_keys=True))
    return 78


if __name__ == "__main__":
    raise SystemExit(main())
