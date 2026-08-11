from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SYSTEMD = ROOT / "deploy" / "systemd"


def test_d7_systemd_units_are_narrow_and_persistent():
    backup = (SYSTEMD / "aemeath-backup.service").read_text(encoding="utf-8")
    backup_timer = (SYSTEMD / "aemeath-backup.timer").read_text(encoding="utf-8")
    prune = (SYSTEMD / "aemeath-log-prune.service").read_text(encoding="utf-8")
    prune_timer = (SYSTEMD / "aemeath-log-prune.timer").read_text(encoding="utf-8")
    health = (SYSTEMD / "aemeath-host-health.service").read_text(encoding="utf-8")
    health_timer = (SYSTEMD / "aemeath-host-health.timer").read_text(encoding="utf-8")

    assert "--profile ops run --rm ops" in backup
    assert "memory" not in backup.lower()
    assert "Persistent=true" in backup_timer
    assert "OnCalendar=*-*-* 03:15:00" in backup_timer

    assert "User=10001" in prune
    assert "--kind app --retention-days 30" in prune
    assert "--kind trace --retention-days 14" in prune
    assert "--apply" in prune
    assert "ReadWritePaths=/var/log/aemeath" in prune
    assert "Persistent=true" in prune_timer

    assert "check_host_health.py" in health
    assert "User=root" in health
    assert "OnUnitActiveSec=5m" in health_timer
    assert "Persistent=true" in health_timer


def test_systemd_units_do_not_embed_credentials_or_provider_commands():
    rendered = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(SYSTEMD.glob("aemeath-*"))
    ).lower()

    assert "deepseek_api_key=" not in rendered
    assert "web_auth_csrf_key=" not in rendered
    assert "authorization:" not in rendered
    assert "memory-worker" not in rendered
    assert "siliconflow" not in rendered
