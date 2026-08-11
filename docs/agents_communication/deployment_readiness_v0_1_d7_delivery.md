# Deployment Readiness v0.1 D7 Delivery

Status: Manager implementation complete; D8 remains pending.

## Scope

D7 adds the minimum host operations required before ECS staging: bounded JSONL retention, scheduled
D6 backup, periodic host health checks, systemd units, and an operator runbook. It does not modify
DialogueController, Character, Memory, RAG, Web business behavior, Persistence schema, quota policy,
or the disabled Memory Worker.

## Delivered Files

- `app/deployment/log_retention.py`
- `app/deployment/host_health.py`
- `scripts/deployment/prune_jsonl.py`
- `scripts/deployment/check_host_health.py`
- `deploy/systemd/aemeath-log-prune.service`
- `deploy/systemd/aemeath-log-prune.timer`
- `deploy/systemd/aemeath-backup.service`
- `deploy/systemd/aemeath-backup.timer`
- `deploy/systemd/aemeath-host-health.service`
- `deploy/systemd/aemeath-host-health.timer`
- `tests/deployment/test_log_retention.py`
- `tests/deployment/test_host_health.py`
- `tests/deployment/test_systemd_contract.py`
- `docs/deployment.md`
- `docs/observability.md`
- `docs/task_log.md`

## Frozen Runtime Policies

- Docker `local` driver remains the stdout/stderr rotation boundary.
- App JSONL retention is 30 days / 512 MiB.
- Trace JSONL retention is 14 days / 1 GiB.
- Prompt Log remains disabled in production.
- Nightly backup runs through the existing networkless D6 `ops` profile.
- Host health checks run every five minutes and output aggregate JSON only.
- No external webhook, email, CloudMonitor, or Sentry credential is stored in repository units.

## Safety Properties

- Prune defaults to dry-run, handles only `.jsonl`, rejects root symlinks, does not follow candidate
  symlinks, protects recently modified files, and checks size/mtime again before unlinking.
- An unreachable size target fails explicitly rather than deleting active files.
- Health log scanning is bounded by file count and bytes; an incomplete scan fails closed.
- Health output contains counts/reason codes only, not log bodies, identifiers, paths, or secrets.
- Backup and host checks retain the controlled root/Docker operator boundary; log prune runs as UID
  and GID 10001 with only `/var/log/aemeath` writable.

## Deferred

- Real ECS installation and `systemctl` evidence.
- CloudMonitor/Sentry/email routing and alert recipients.
- Off-ECS backup copy and backup/quarantine pruning.
- D5 Memory Worker gates.

These are D8 or separately approved follow-ups and do not weaken the D7 local contract.

## Validation

- D7 focused tests passed, with only platform-dependent symlink cases skipped where unavailable.
- All deployment tests passed with their existing environment-dependent skips.
- Full `pytest -q -p no:cacheprovider` regression passed with existing deprecation warnings only.
- `python -m ruff check .` passed.
- `python -m compileall -q app scripts tests` passed.
- `python scripts/deployment/check_deploy_lock.py` passed.
- `git diff --check` passed with Windows LF/CRLF notices only.
- Ubuntu 22.04 `systemd-analyze verify` passed after copying units to a Linux-permission temporary
  directory and providing a verification-only `docker.service` dependency stub. The remaining
  `snapd.service` warning belongs to the WSL host image.
- A live isolated Compose smoke returned `status=ok` for API/proxy health, disk capacity, D6 backup
  freshness, and bounded recent App Log signals. The production App retention policy also completed
  a dry-run without deleting files.
