from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PATH = PROJECT_ROOT / "compose.yaml"
CADDY_DOCKERFILE_PATH = PROJECT_ROOT / "deploy" / "Caddy.Dockerfile"
CADDY_CONTEXT_IGNORE_PATH = PROJECT_ROOT / "deploy" / "Caddy.Dockerfile.dockerignore"
PROD_ENV_EXAMPLE_PATH = PROJECT_ROOT / "deploy" / "prod.env.example"


def _compose_document() -> dict[str, object]:
    return yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))


def _volume_map(service: dict[str, object]) -> dict[str, dict[str, object]]:
    return {item["target"]: item for item in service.get("volumes", [])}


def test_compose_default_topology_and_ingress_are_narrow():
    document = _compose_document()
    services = document["services"]
    default_services = {name for name, service in services.items() if "profiles" not in service}

    assert set(services) == {"api", "proxy", "memory-worker", "ops", "restore"}
    assert default_services == {"api", "proxy"}
    assert services["api"]["expose"] == ["8000"]
    assert "ports" not in services["api"]
    assert services["proxy"]["ports"] == [
        {"target": 8080, "published": "80", "protocol": "tcp"},
        {"target": 8443, "published": "443", "protocol": "tcp"},
    ]
    assert all("ports" not in services[name] for name in {"memory-worker", "ops", "restore"})
    assert services["proxy"]["depends_on"] == {"api": {"condition": "service_healthy"}}


def test_compose_security_network_and_logging_contract():
    document = _compose_document()
    services = document["services"]
    caddy_base_image = next(
        line
        for line in (PROJECT_ROOT / "deploy" / "caddy-image.lock")
        .read_text(encoding="utf-8")
        .splitlines()
        if line and not line.startswith("#")
    )

    for service in services.values():
        assert service["read_only"] is True
        assert service["init"] is True
        assert service["cap_drop"] == ["ALL"]
        assert service["security_opt"] == ["no-new-privileges:true"]
        assert service["logging"] == {
            "driver": "local",
            "options": {"max-size": "10m", "max-file": "5"},
        }
    assert services["api"]["user"] == "10001:10001"
    assert services["api"]["environment"]["AEMEATH_API_AUTH_MODE"] == "web_session"
    assert services["api"]["environment"]["AEMEATH_WEB_AUTH_CSRF_KEY_FILE"] == (
        "/run/secrets/web_auth_csrf_key"
    )
    assert "AEMEATH_DEV_AUTH_TOKEN_FILE" not in services["api"]["environment"]
    assert services["proxy"]["user"] == "10002:10002"
    assert services["proxy"]["image"] == "aemeath-caddy:2.10.2-nonroot"
    assert services["proxy"]["build"] == {
        "context": ".",
        "dockerfile": "deploy/Caddy.Dockerfile",
    }
    caddy_dockerfile = CADDY_DOCKERFILE_PATH.read_text(encoding="utf-8")
    assert f"FROM {caddy_base_image}" in caddy_dockerfile
    assert "setcap -r /usr/bin/caddy" in caddy_dockerfile
    assert "chown -R 10002:10002 /data /config" in caddy_dockerfile
    assert caddy_dockerfile.rstrip().endswith("USER 10002:10002")
    assert "COPY --chown=10002:10002 web/dist /srv/aemeath-web" in caddy_dockerfile
    caddy_ignore = CADDY_CONTEXT_IGNORE_PATH.read_text(encoding="utf-8")
    assert "!web/dist/**" in caddy_ignore
    assert services["proxy"]["networks"] == {"frontend": {"ipv4_address": "172.30.0.10"}}
    assert document["networks"]["frontend"]["ipam"]["config"] == [
        {"subnet": "172.30.0.0/24"}
    ]
    assert services["api"]["environment"]["AEMEATH_TRUSTED_PROXY_IPS"] == "172.30.0.10"


def test_proxy_healthcheck_covers_caddy_and_provider_free_upstream_health():
    services = _compose_document()["services"]
    healthcheck = services["proxy"]["healthcheck"]

    assert healthcheck["test"][0] == "CMD-SHELL"
    command = healthcheck["test"][1]
    assert "http://127.0.0.1:2019/config/" in command
    assert "http://api:8000/health" in command
    assert "&&" in command
    assert "/ready" not in command
    assert "/v1/" not in command


def test_compose_volume_and_secret_scope_is_least_privilege():
    document = _compose_document()
    services = document["services"]
    api_volumes = _volume_map(services["api"])
    proxy_volumes = _volume_map(services["proxy"])
    ops_volumes = _volume_map(services["ops"])
    restore_volumes = _volume_map(services["restore"])

    assert api_volumes == {
        "/var/lib/aemeath/data": {
            "type": "bind",
            "source": "/var/lib/aemeath/data",
            "target": "/var/lib/aemeath/data",
        },
        "/var/log/aemeath": {
            "type": "bind",
            "source": "/var/log/aemeath",
            "target": "/var/log/aemeath",
        },
    }
    assert set(proxy_volumes) == {"/data", "/config"}
    assert ops_volumes["/var/lib/aemeath/data"]["read_only"] is True
    assert ops_volumes["/var/log/aemeath"]["read_only"] is True
    assert "read_only" not in ops_volumes["/var/lib/aemeath/rag_indexes"]
    assert "read_only" not in ops_volumes["/var/backups/aemeath"]
    assert "read_only" not in restore_volumes["/var/lib/aemeath/data"]
    assert restore_volumes["/var/lib/aemeath/rag_indexes"]["read_only"] is True
    assert restore_volumes["/var/backups/aemeath"]["read_only"] is True

    assert services["api"]["secrets"] == [
        {"source": "deepseek_api_key", "target": "deepseek_api_key"},
        {"source": "web_auth_csrf_key", "target": "web_auth_csrf_key"},
    ]
    assert document["secrets"] == {
        "deepseek_api_key": {"file": "/etc/aemeath/secrets/deepseek_api_key"},
        "web_auth_csrf_key": {"file": "/etc/aemeath/secrets/web_auth_csrf_key"},
    }
    services_without_secrets = {"proxy", "memory-worker", "ops", "restore"}
    assert all("secrets" not in services[name] for name in services_without_secrets)
    assert "siliconflow" not in COMPOSE_PATH.read_text(encoding="utf-8").lower()


def test_profiles_are_explicit_and_d6_ops_restore_are_enabled():
    document = _compose_document()
    services = document["services"]

    assert services["memory-worker"]["profiles"] == ["memory"]
    assert services["ops"]["profiles"] == ["ops"]
    assert services["restore"]["profiles"] == ["restore"]
    for service_name in ("memory-worker", "ops", "restore"):
        service = services[service_name]
        assert service["network_mode"] == "none"
        assert service["restart"] == "no"
    assert services["memory-worker"]["command"] == [
        "python",
        "/opt/aemeath/scripts/deployment/profile_guard.py",
        "memory-worker",
    ]
    assert services["ops"]["command"] == [
        "python",
        "/opt/aemeath/scripts/deployment/backup_sqlite.py",
        "--db-path",
        "/var/lib/aemeath/data/aemeath.db",
        "--backup-root",
        "/var/backups/aemeath",
        "--backend",
        "sqlite_raw",
        "--label",
        "nightly",
        "--app-image-digest",
        "${AEMEATH_APP_IMAGE_DIGEST:-unknown}",
        "--ops-image-digest",
        "${AEMEATH_OPS_IMAGE_DIGEST:-unknown}",
        "--deploy-lock-sha256",
        "${AEMEATH_DEPLOY_LOCK_SHA256:-unknown}",
        "--compose-config-sha256",
        "${AEMEATH_COMPOSE_CONFIG_SHA256:-unknown}",
        "--git-commit",
        "${AEMEATH_BUILD_COMMIT:-unknown}",
    ]
    assert services["restore"]["command"] == [
        "python",
        "/opt/aemeath/scripts/deployment/restore_sqlite.py",
    ]
    compose_text = COMPOSE_PATH.read_text(encoding="utf-8")
    assert "app.memory.worker" not in compose_text
    for variable in (
        "AEMEATH_APP_IMAGE_DIGEST",
        "AEMEATH_OPS_IMAGE_DIGEST",
        "AEMEATH_DEPLOY_LOCK_SHA256",
        "AEMEATH_COMPOSE_CONFIG_SHA256",
        "AEMEATH_BUILD_COMMIT",
    ):
        assert f"${{{variable}:?" not in compose_text
    assert "include" not in document
    assert "extends" not in document


def test_production_env_template_is_tracked_release_asset():
    template = PROD_ENV_EXAMPLE_PATH.read_text(encoding="utf-8")

    assert "AEMEATH_APP_IMAGE_DIGEST=" in template
    assert "AEMEATH_OPS_IMAGE_DIGEST=" in template
    assert "AEMEATH_DEPLOY_LOCK_SHA256=" in template
    assert "AEMEATH_COMPOSE_CONFIG_SHA256=" in template
    assert "AEMEATH_BUILD_COMMIT=" in template
    assert "AEMEATH_USAGE_QUOTA_ENABLED=true" in template
    assert "AEMEATH_USAGE_QUOTA_DAILY_TOKEN_LIMIT=200000" in template
    assert "AEMEATH_USAGE_QUOTA_ESTIMATOR_VERSION=prompt_utf8_v0_1" in template
    assert "AEMEATH_API_AUTH_MODE=web_session" in template
    assert "AEMEATH_WEB_AUTH_CSRF_KEY_FILE=/run/secrets/web_auth_csrf_key" in template
    assert "AEMEATH_DEV_AUTH_TOKEN_FILE" not in template
    assert "DEEPSEEK_API_KEY=" not in template


@pytest.mark.parametrize(
    ("profile", "reason"),
    [
        ("memory-worker", "memory_worker_gates_not_approved"),
        ("ops", "profile_not_allowed"),
        ("restore", "profile_not_allowed"),
    ],
)
def test_profile_guard_returns_safe_nonzero_status(profile: str, reason: str):
    completed = subprocess.run(
        [sys.executable, "scripts/deployment/profile_guard.py", profile],
        cwd=PROJECT_ROOT,
        capture_output=True,
        check=False,
        text=True,
        encoding="utf-8",
    )

    assert completed.returncode == 78
    assert json.loads(completed.stdout) == {"status": "disabled", "reason": reason}
    assert completed.stderr == ""


@pytest.mark.skipif(shutil.which("docker") is None, reason="Docker CLI is unavailable")
def test_docker_compose_default_config_does_not_require_d6_identity():
    environment = os.environ.copy()
    for key in (
        "AEMEATH_APP_IMAGE_DIGEST",
        "AEMEATH_OPS_IMAGE_DIGEST",
        "AEMEATH_DEPLOY_LOCK_SHA256",
        "AEMEATH_COMPOSE_CONFIG_SHA256",
        "AEMEATH_BUILD_COMMIT",
    ):
        environment.pop(key, None)
    environment.update(
        {
            "AEMEATH_DOMAIN": "agent.example.test",
            "AEMEATH_ENV_FILE": str(PROD_ENV_EXAMPLE_PATH),
            "AEMEATH_IMAGE": "aemeath-agent:d4-contract-test",
        }
    )

    completed = subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE_PATH), "config", "-q"],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
        encoding="utf-8",
    )

    assert completed.returncode == 0
    assert completed.stdout == ""
    assert completed.stderr == ""


@pytest.mark.skipif(shutil.which("docker") is None, reason="Docker CLI is unavailable")
def test_docker_compose_cli_accepts_only_safe_queries():
    environment = os.environ.copy()
    environment.update(
        {
            "AEMEATH_DOMAIN": "agent.example.test",
            "AEMEATH_ENV_FILE": str(PROD_ENV_EXAMPLE_PATH),
            "AEMEATH_IMAGE": "aemeath-agent:d4-contract-test",
            "AEMEATH_APP_IMAGE_DIGEST": "sha256:" + "a" * 64,
            "AEMEATH_OPS_IMAGE_DIGEST": "sha256:" + "b" * 64,
            "AEMEATH_DEPLOY_LOCK_SHA256": "c" * 64,
            "AEMEATH_COMPOSE_CONFIG_SHA256": "d" * 64,
            "AEMEATH_BUILD_COMMIT": "e" * 40,
        }
    )
    base_command = ["docker", "compose", "-f", str(COMPOSE_PATH)]

    validated = subprocess.run(
        [*base_command, "config", "-q"],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
        encoding="utf-8",
    )
    services = subprocess.run(
        [*base_command, "config", "--services"],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
        encoding="utf-8",
    )
    profiles = subprocess.run(
        [*base_command, "config", "--profiles"],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
        encoding="utf-8",
    )

    assert validated.returncode == 0
    assert validated.stdout == ""
    assert services.returncode == 0
    assert set(services.stdout.splitlines()) == {"api", "proxy"}
    assert profiles.returncode == 0
    assert set(profiles.stdout.splitlines()) == {"memory", "ops", "restore"}
    assert validated.stderr == services.stderr == profiles.stderr == ""


def test_operator_runbook_uses_sudo_for_root_only_production_configuration():
    runbook = (PROJECT_ROOT / "docs" / "deployment.md").read_text(encoding="utf-8")

    assert "root:root `0640`" in runbook
    assert "sudo docker compose --env-file /etc/aemeath/prod.env" in runbook
    assert "\ndocker compose --env-file /etc/aemeath/prod.env" not in runbook
