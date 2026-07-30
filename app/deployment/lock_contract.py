from __future__ import annotations

import re
from dataclasses import dataclass

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

_LOCK_ENTRY_RE = re.compile(r"(?m)^([A-Za-z0-9_.-]+)([^\r\n]*)")
_LOCK_PIN_RE = re.compile(r"^[A-Za-z0-9_.-]+==([^\s;\\]+)")
_QUOTED_VALUE_RE = re.compile(r'["\']([^"\']+)["\']')
_PYTHON_310_REQUIRED_PACKAGES = frozenset({"exceptiongroup"})
_UVICORN_STANDARD_PACKAGES = frozenset({"httptools", "uvloop", "watchfiles", "websockets"})


@dataclass(frozen=True)
class DeploymentLockReport:
    missing_packages: tuple[str, ...]
    constraint_mismatches: tuple[str, ...]
    unpinned_packages: tuple[str, ...]
    unhashed_packages: tuple[str, ...]

    @property
    def valid(self) -> bool:
        return not (
            self.missing_packages
            or self.constraint_mismatches
            or self.unpinned_packages
            or self.unhashed_packages
        )


def validate_deployment_lock(*, pyproject_text: str, lock_text: str) -> DeploymentLockReport:
    direct_requirements = _requirements_from_array(
        pyproject_text,
        section="project",
        key="dependencies",
    )
    api_requirements = _requirements_from_array(
        pyproject_text,
        section="project.optional-dependencies",
        key="api",
    )
    declared_requirements = direct_requirements + api_requirements
    required_names = {canonicalize_name(item.name) for item in declared_requirements}
    required_names.update(_PYTHON_310_REQUIRED_PACKAGES)
    for requirement in api_requirements:
        if canonicalize_name(requirement.name) == "uvicorn" and "standard" in requirement.extras:
            required_names.update(_UVICORN_STANDARD_PACKAGES)

    lock_entries = _lock_entries(lock_text)
    locked_names = set(lock_entries)
    locked_versions = {
        name: version
        for name, entry in lock_entries.items()
        if (version := _locked_version(entry)) is not None
    }
    constraint_mismatches = {
        canonicalize_name(requirement.name)
        for requirement in declared_requirements
        if (version := locked_versions.get(canonicalize_name(requirement.name))) is not None
        and version not in requirement.specifier
    }
    return DeploymentLockReport(
        missing_packages=tuple(sorted(required_names - locked_names)),
        constraint_mismatches=tuple(sorted(constraint_mismatches)),
        unpinned_packages=tuple(
            sorted(name for name in lock_entries if name not in locked_versions)
        ),
        unhashed_packages=tuple(
            sorted(name for name, entry in lock_entries.items() if "--hash=sha256:" not in entry)
        ),
    )


def _requirements_from_array(
    document: str,
    *,
    section: str,
    key: str,
) -> list[Requirement]:
    current_section = ""
    collecting = False
    values: list[str] = []
    for raw_line in document.splitlines():
        line = raw_line.strip()
        if line.startswith("[") and line.endswith("]"):
            current_section = line[1:-1].strip()
            collecting = False
            continue
        if current_section != section:
            continue
        if not collecting:
            if not re.match(rf"^{re.escape(key)}\s*=\s*\[", line):
                continue
            collecting = True
        values.extend(_QUOTED_VALUE_RE.findall(line))
        if "]" in line:
            break

    return [Requirement(value) for value in values]


def _lock_entries(document: str) -> dict[str, str]:
    entries: dict[str, str] = {}
    starts = list(_LOCK_ENTRY_RE.finditer(document))
    for index, match in enumerate(starts):
        if match.group(1).startswith("sha256"):
            continue
        end = starts[index + 1].start() if index + 1 < len(starts) else len(document)
        entry = document[match.start() : end]
        entries[canonicalize_name(match.group(1))] = entry
    return entries


def _locked_version(entry: str) -> Version | None:
    match = _LOCK_PIN_RE.match(entry)
    if match is None:
        return None
    try:
        return Version(match.group(1))
    except InvalidVersion:
        return None
