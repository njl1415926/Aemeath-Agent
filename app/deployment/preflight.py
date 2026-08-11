from __future__ import annotations

import errno
import os
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path

from app.config import Settings
from app.deployment.secrets import (
    API_BEARER_SECRET_PATH,
    DEEPSEEK_SECRET_PATH,
    WEB_AUTH_CSRF_SECRET_PATH,
)
from app.deployment.uvicorn_config import validated_trusted_proxy_ips

_FORBIDDEN_SILICONFLOW_SECRET = Path("/run/secrets/siliconflow_api_key")


@dataclass(frozen=True)
class PreflightReport:
    environment: str
    checks: tuple[str, ...]


def run_startup_preflight(settings: Settings) -> PreflightReport:
    """Validate the API deployment before any runtime opens the database."""
    settings.validate()
    checks = ["configuration"]
    if settings.environment != "prod":
        return PreflightReport(environment=settings.environment, checks=tuple(checks))

    app_root = settings.deployment_app_root
    data_root = settings.db_path.parent
    log_roots = (
        settings.app_log_root,
        settings.trace_log_root,
        settings.prompt_run_log_root,
    )
    _require_directory(app_root, writable=False, name="application_root")
    _require_read_only_directory(app_root)
    if (app_root / ".env").exists():
        raise ValueError("application_root must not contain a dotenv file.")
    _require_image_resource(app_root, settings.character_card_path, "character_resource")
    _require_image_resource(app_root, settings.prompt_template_path, "prompt_resource")
    checks.append("immutable_resources")

    _require_directory(data_root, writable=True, name="data_root")
    _require_database_target(settings.db_path)
    for index, root in enumerate(log_roots):
        _require_directory(root, writable=True, name=f"log_root_{index}")
    checks.append("state_volumes")

    secret_files = {"DEEPSEEK_API_KEY_FILE": DEEPSEEK_SECRET_PATH}
    if settings.api_auth_mode == "web_session":
        secret_files["AEMEATH_WEB_AUTH_CSRF_KEY_FILE"] = WEB_AUTH_CSRF_SECRET_PATH
    else:
        secret_files["AEMEATH_DEV_AUTH_TOKEN_FILE"] = API_BEARER_SECRET_PATH
    for env_name, approved_path in secret_files.items():
        _require_secret_file(env_name, approved_path=approved_path)
    if "SILICONFLOW_API_KEY" in os.environ or "SILICONFLOW_API_KEY_FILE" in os.environ:
        raise ValueError("SiliconFlow secret configuration is forbidden in API-only prod v0.1.")
    if _FORBIDDEN_SILICONFLOW_SECRET.exists():
        raise ValueError("SiliconFlow secret mount is forbidden in API-only prod v0.1.")
    checks.append("secret_mounts")
    validated_trusted_proxy_ips(
        os.environ.get("AEMEATH_TRUSTED_PROXY_IPS"),
        environment=settings.environment,
    )
    checks.append("trusted_proxy")

    return PreflightReport(environment=settings.environment, checks=tuple(checks))


def _require_image_resource(app_root: Path, configured: Path, name: str) -> None:
    try:
        candidate = configured if configured.is_absolute() else app_root / configured
        resolved_root = app_root.resolve(strict=True)
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"{name} is unavailable.") from exc
    if not resolved.is_relative_to(resolved_root) or not resolved.exists():
        raise ValueError(f"{name} must resolve inside application_root.")
    if not os.access(resolved, os.R_OK):
        raise ValueError(f"{name} must be readable.")


def _require_directory(path: Path, *, writable: bool, name: str) -> None:
    _reject_symlink_components(path, name)
    try:
        info = path.stat()
    except OSError as exc:
        raise ValueError(f"{name} is unavailable.") from exc
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError(f"{name} must be a directory.")
    if os.name != "nt" and info.st_mode & stat.S_IWOTH:
        raise ValueError(f"{name} must not be world-writable.")
    required_access = os.R_OK | os.X_OK | (os.W_OK if writable else 0)
    if not os.access(path, required_access):
        raise ValueError(f"{name} has insufficient access.")
    _require_current_identity_access(info, writable=writable, name=name)
    if writable:
        _sentinel_write(path, name)


def _require_read_only_directory(path: Path) -> None:
    sentinel = path / f".aemeath-readonly-check-{uuid.uuid4().hex}"
    descriptor: int | None = None
    try:
        descriptor = os.open(sentinel, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except OSError as exc:
        if exc.errno in {errno.EACCES, errno.EPERM, errno.EROFS}:
            return
        raise ValueError("application_root write protection could not be verified.") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    try:
        sentinel.unlink(missing_ok=True)
    except OSError as exc:
        raise ValueError("application_root write protection check cleanup failed.") from exc
    raise ValueError("application_root must be mounted read-only.")


def _reject_symlink_components(path: Path, name: str) -> None:
    try:
        absolute = path.absolute()
        for candidate in (absolute, *absolute.parents):
            if candidate.exists() and candidate.is_symlink():
                raise ValueError(f"{name} must not contain symlink components.")
    except OSError as exc:
        raise ValueError(f"{name} could not be inspected.") from exc


def _require_current_identity_access(
    info: os.stat_result,
    *,
    writable: bool,
    name: str,
) -> None:
    if not hasattr(os, "geteuid"):
        return
    effective_uid = os.geteuid()
    if effective_uid == 0:
        raise ValueError("Production API must not run as root.")
    groups = {os.getegid(), *os.getgroups()}
    if info.st_uid == effective_uid:
        mode_mask = stat.S_IRUSR | stat.S_IXUSR | (stat.S_IWUSR if writable else 0)
    elif info.st_gid in groups:
        mode_mask = stat.S_IRGRP | stat.S_IXGRP | (stat.S_IWGRP if writable else 0)
    else:
        raise ValueError(f"{name} is not owned by the runtime identity or its groups.")
    if info.st_mode & mode_mask != mode_mask:
        raise ValueError(f"{name} mode does not grant the required access.")


def _sentinel_write(path: Path, name: str) -> None:
    sentinel = path / f".aemeath-preflight-{uuid.uuid4().hex}"
    descriptor: int | None = None
    try:
        descriptor = os.open(sentinel, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        sentinel.unlink()
        _fsync_directory(path)
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        try:
            sentinel.unlink(missing_ok=True)
        except OSError:
            pass
        raise ValueError(f"{name} failed the sentinel write check.") from exc


def _require_database_target(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise ValueError("database_target is unavailable.") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ValueError("database_target must be a regular non-symlink file.")
    if not os.access(path, os.R_OK | os.W_OK):
        raise ValueError("database_target has insufficient access.")


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _require_secret_file(env_name: str, *, approved_path: Path) -> None:
    raw_path = os.environ.get(env_name)
    if not raw_path:
        raise ValueError(f"{env_name} is required in prod.")
    path = Path(raw_path)
    if path != approved_path:
        raise ValueError(f"{env_name} must use the approved production path.")
    _reject_symlink_components(path, env_name)
    try:
        info = path.lstat()
    except OSError as exc:
        raise ValueError(f"{env_name} is unavailable.") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ValueError(f"{env_name} must reference a regular non-symlink file.")
    if os.name != "nt":
        _require_linux_secret_contract(info, env_name=env_name)
        if os.access(path, os.W_OK, effective_ids=True):
            raise ValueError(f"{env_name} must not be writable by the runtime identity.")
    if not os.access(path, os.R_OK):
        raise ValueError(f"{env_name} is not readable by the runtime identity.")


def _require_linux_secret_contract(info: os.stat_result, *, env_name: str) -> None:
    if not all(hasattr(os, name) for name in ("geteuid", "getegid", "getgroups")):
        raise ValueError(f"{env_name} runtime identity could not be verified.")
    if os.geteuid() != 10001 or os.getegid() != 10001:
        raise ValueError(f"{env_name} requires runtime identity 10001:10001.")
    if info.st_uid != 0 or info.st_gid != 10001 or stat.S_IMODE(info.st_mode) != 0o440:
        raise ValueError(f"{env_name} must be owned by root:10001 with mode 0440.")
    if 10001 not in {os.getegid(), *os.getgroups()}:
        raise ValueError(f"{env_name} is not readable by the approved runtime group.")
