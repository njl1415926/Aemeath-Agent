from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

_TERMINAL_DENY_RULES = {
    "**/.env",
    "**/.env.*",
    "**/*.db",
    "**/*.db-*",
    "**/*.sqlite",
    "**/*.sqlite-*",
    "**/*.sqlite3",
    "**/*.sqlite3-*",
    "**/*.jsonl",
    "**/.aemeath",
    "**/.aemeath/**",
    "**/.cache",
    "**/.cache/**",
    "/app/data",
    "/app/data/**",
    "/app/rag_indexes",
    "/app/rag_indexes/**",
    "**/*.key",
    "**/*.pem",
    "**/*.p12",
    "**/*.pfx",
}


def test_dockerignore_terminal_rules_cannot_be_undone_by_app_allowlist():
    lines = [
        line.strip()
        for line in (PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    app_allow_index = lines.index("!app/**")
    last_deny_index = max(lines.index(rule) for rule in _TERMINAL_DENY_RULES)

    assert _TERMINAL_DENY_RULES <= set(lines)
    assert all(lines.index(rule) > app_allow_index for rule in _TERMINAL_DENY_RULES)
    assert not any(line.startswith("!") for line in lines[last_deny_index + 1 :])


def test_dockerfile_copies_only_approved_context_roots():
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "COPY . " not in dockerfile
    assert "COPY requirements.deploy.lock /tmp/requirements.deploy.lock" in dockerfile
    assert "COPY --chown=10001:10001 app /opt/aemeath/app" in dockerfile
    assert (
        "COPY --chown=10001:10001 scripts/deployment /opt/aemeath/scripts/deployment"
        in dockerfile
    )
    assert "--only-binary=:all:" in dockerfile
