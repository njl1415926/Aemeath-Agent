from __future__ import annotations

import os
from pathlib import Path

_MAX_SECRET_BYTES = 8192
DEEPSEEK_SECRET_PATH = Path("/run/secrets/deepseek_api_key")
API_BEARER_SECRET_PATH = Path("/run/secrets/api_bearer_token")


def resolve_secret(
    value_name: str,
    file_name: str,
    *,
    environment: str,
    approved_file_path: Path | None = None,
) -> str | None:
    """Resolve one secret without exposing its value in errors or logs."""
    value_is_set = value_name in os.environ and os.environ[value_name] != ""
    file_is_set = file_name in os.environ
    if value_is_set and file_is_set:
        raise ValueError(f"{value_name} and {file_name} cannot both be configured.")
    if environment == "prod" and value_is_set:
        raise ValueError(f"{value_name} must use {file_name} in prod.")
    if file_is_set:
        path = Path(os.environ[file_name])
        if environment == "prod" and approved_file_path is not None and path != approved_file_path:
            raise ValueError(f"{file_name} must use the approved production path.")
        return _read_secret_file(file_name, path)
    if not value_is_set:
        return None
    return _validate_secret_text(value_name, os.environ[value_name])


def _read_secret_file(name: str, path: Path) -> str:
    try:
        with path.open("rb") as handle:
            raw = handle.read(_MAX_SECRET_BYTES + 1)
    except (OSError, ValueError) as exc:
        raise ValueError(f"{name} could not be read.") from exc
    if len(raw) > _MAX_SECRET_BYTES:
        raise ValueError(f"{name} exceeds the secret size limit.")
    try:
        value = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{name} must contain strict UTF-8.") from exc
    if value.endswith("\r\n"):
        value = value[:-2]
    elif value.endswith("\n"):
        value = value[:-1]
    return _validate_secret_text(name, value)


def _validate_secret_text(name: str, value: str) -> str:
    if not value:
        raise ValueError(f"{name} must not be empty.")
    if "\x00" in value or "\r" in value or "\n" in value:
        raise ValueError(f"{name} contains a forbidden control character.")
    return value
