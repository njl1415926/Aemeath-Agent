# Deployment v0.1 - D1-D4 and Web Phase 3 Lite operator contract

Status: D1–D4 accepted; T-0140 remains pending.
This is not yet a complete ECS runbook because backup/restore, retention, Memory Worker gates,
and ECS validation remain later work.

## Fixed first-release boundary

- Ubuntu 22.04 x86_64 / `linux/amd64`, one ECS host, one API container and one Uvicorn worker.
- Caddy is the only future public entry point. Port 8000 must stay private; only 80/443 may be
  published after D4.
- Long-term Memory is disabled and no Memory Worker or SiliconFlow secret is allowed.
- The active evidence path remains `WorldInfoEvidenceProvider`; no vector RAG is activated.
- Context Budget is enabled, CORS is empty, model fallback/mock/local default user/hash embedding
  are disabled, and the public profile uses invitation-only Cookie Sessions with daily quota.

## Image contract

`Dockerfile` uses the linux/amd64 manifest digest for Python 3.10.20 slim-bookworm in both stages.
`requirements.deploy.lock` pins each runtime/API dependency and artifact hash, including all four
Linux packages selected by `uvicorn[standard]`: `httptools`, `uvloop`, `watchfiles`, and
`websockets`. The Docker build requires wheels with `--only-binary=:all:`. Before building, run
`python scripts/deployment/check_deploy_lock.py`; it fails if the lock omits a project/API direct
dependency or Uvicorn standard package, if a locked direct-dependency version does not satisfy its
PEP 440 project constraint, if the Python 3.10 `exceptiongroup` conditional dependency is absent,
or if an entry is unpinned/unhashed. The final image has no compiler or package installer, runs as
UID/GID `10001:10001`, uses UTF-8 and UTC, writes no bytecode, and exposes only container port 8000.
Application resources are copied under `/opt/aemeath`; DB and JSONL mount points are outside that
root.

Build from WSL2 Ubuntu 22.04:

```bash
docker build --platform linux/amd64 \
  --build-arg BUILD_COMMIT="$(git rev-parse HEAD)" \
  --tag aemeath-agent:5b1fe0b-d1d3 .
docker image inspect aemeath-agent:5b1fe0b-d1d3 \
  --format '{{.Config.User}} {{json .Config.Cmd}}'
```

Expected user is `10001:10001`; the command is
`python /opt/aemeath/scripts/deployment/start_api.py`. Runtime must add `--read-only`, a writable
`/tmp` tmpfs, and the approved data/log mounts. The production preflight rejects a writable
`/opt/aemeath`.

## Production configuration and secrets

Start from `deploy/prod.env.example`, copy it outside the repository, and set the approved
non-default user ID and exact Caddy container IP. Do not pass that example directly while required
fields are blank. Production rejects any drift from the frozen Web profile. Only dev may load
dotenv, and dotenv is not allowed to switch the initially selected environment to test or prod.

Secrets are file mounts, not environment values:

| Variable | Required container file | Purpose |
|---|---|---|
| `DEEPSEEK_API_KEY_FILE` | `/run/secrets/deepseek_api_key` | DeepSeek chat API |
| `AEMEATH_WEB_AUTH_CSRF_KEY_FILE` | `/run/secrets/web_auth_csrf_key` | Cookie Session CSRF derivation |

`_FILE` is an Aemeath application convention; Docker Compose does not automatically translate it.
The resolver rejects plaintext plus `_FILE`, production plaintext, any path other than an approved
fixed path, legacy `OPENAI_API_KEY`, empty or oversized content, invalid UTF-8, NUL, and
residual CR/LF. It removes one final LF or one final CRLF and preserves all other characters.
Production preflight rejects symlinks in the file or any parent component and requires Linux
runtime identity `10001:10001`, file owner/group `root:10001`, and exact mode `0440`; the runtime
UID/GID therefore cannot modify either secret. Compose secrets are mounted files, not a general
encrypted secret store; review the [Docker Compose secrets contract](https://docs.docker.com/compose/how-tos/use-secrets/)
and its [trust model](https://docs.docker.com/compose/trust-model/).

Never save or upload an unredacted `docker compose config` rendering. D2 supplies
`scripts/deployment/check_secret_marker.py` for controlled scans with a unique fake marker; it
reports only category plus pass/fail and never prints the marker.

## Preflight and startup

The standalone check and `create_app()` both execute the same contract:

```bash
python scripts/deployment/preflight.py
python scripts/deployment/start_api.py
```

In production, preflight verifies the frozen flags, HTTPS DeepSeek origin, exact volume paths,
read-only application resources, readable character/prompt resources, non-root identity,
non-world-writable data/log roots, sentinel write/fsync/delete on writable mounts, restrictive
secret files, absence of `/opt/aemeath/.env`, and absence of SiliconFlow configuration/mounts.
Output is limited to safe check names or a configuration reason; it never displays secret values.
Non-`ValueError` configuration failures are reduced to the fixed `configuration_invalid` reason
without a traceback.

The launcher always uses host `0.0.0.0`, port `8000`, `workers=1`, no reload, no Uvicorn access
log, a 60-second graceful timeout, and only the exact `AEMEATH_TRUSTED_PROXY_IPS` value. Docker
should allow at least 75 seconds before SIGKILL. Lifespan marks readiness `closing` and then calls
the existing synchronous `ApplicationRuntime.close()`.

## Health, readiness, CORS, and proxy

- `/health`: fixed liveness response; no DB/filesystem/Provider access.
- `/ready`: `starting`, `ready`, and `closing` lifecycle plus read-only SQLite and cached filesystem
  checks. Both `sqlite_raw` and the existing dev/test SQLAlchemy-on-SQLite backend use this narrow
  file probe with backend-appropriate schema markers; production still rejects SQLAlchemy. Default
  total budget is 500 ms, DB budget 200 ms, filesystem TTL 5 seconds. It never calls a paid Provider
  and never returns paths, SQL, exceptions, or counts.
- Both probes are unauthenticated, minimal, and return `Cache-Control: no-store`.
- Production CORS is an empty JSON list. Dev/test allowlists are exact origins only; wildcard,
  userinfo, path, query, and fragment are rejected.

`deploy/Caddyfile` overwrites inbound `X-Forwarded-*`, applies `request_body max_size 128KiB` to API
requests, denies synchronous `POST /v1/chat`, serves the Vite SPA, and has no access-log directive.
It applies the reviewed CSP, frame, content-type, referrer, and permissions headers. HSTS remains a
D8 public-domain decision. The D3 Caddy image reference is pinned in
`deploy/caddy-image.lock`. Caddy documents `request_body` as experimental; Manager explicitly
accepted this risk, but actual Content-Length/chunked boundary tests remain mandatory in D4/D8.
See the [Caddy request body directive](https://caddyserver.com/docs/caddyfile/directives/request_body)
and [Uvicorn proxy settings](https://www.uvicorn.org/settings/).

## Docker Compose D4

`compose.yaml` has two default resident services. `api` is the single Uvicorn application process,
has no published host port, and is reachable only as `api:8000` on `aemeath_frontend`. `proxy` is
derived reproducibly by `deploy/Caddy.Dockerfile` from the approved digest-pinned Caddy base. The
small derived image creates numeric UID/GID `10002:10002`, removes the upstream low-port file
capability that is unnecessary on 8080/8443, and makes only `/data` and `/config` writable; the
upstream image has no named non-root user. Proxy uses fixed address `172.30.0.10`,
listens as non-root on container ports 8080/8443, and is the only service publishing host ports
80/443. Both resident services use a read-only root filesystem, dropped capabilities,
`no-new-privileges`, a bounded `/tmp` tmpfs, Docker's rotating
`local` log driver, and explicit graceful-stop windows.
`deploy/Caddy.Dockerfile` copies only the reviewed Caddyfile and prebuilt `web/dist`. Run `npm ci`,
`npm test`, and `npm run build` in `web/` before building the proxy image. Vite emits content-hashed
JavaScript, CSS, avatar, and background assets. Only `/assets/*` receives immutable caching;
`index.html` remains `no-cache`.

Create production host paths before the first start:

```bash
sudo install -d -o 10001 -g 10001 -m 0750 \
  /var/lib/aemeath/data /var/lib/aemeath/rag_indexes \
  /var/log/aemeath /var/log/aemeath/app \
  /var/log/aemeath/trace /var/log/aemeath/prompt
sudo install -d -o root -g root -m 0750 /etc/aemeath /etc/aemeath/secrets
sudo install -d -o root -g root -m 0750 /var/backups/aemeath
sudo install -o root -g root -m 0640 deploy/prod.env.example /etc/aemeath/prod.env
```

The first-release operator contract is controlled root execution: keep the environment file
root:root `0640`, keep `/etc/aemeath` root-traversable only, edit the non-secret configuration with
`sudoedit /etc/aemeath/prod.env`, and run every production Compose command through `sudo`. Do not
make the environment file or Secret paths world-readable and do not treat membership in the Docker
group as permission to bypass this contract.

Set `AEMEATH_DOMAIN`, `AEMEATH_WEB_PUBLIC_ORIGIN`, `AEMEATH_DEFAULT_USER_ID`, and an immutable
`AEMEATH_IMAGE` tag or digest. The public origin must be the exact HTTPS origin for the domain. The
invitation-test quota is frozen at 50 turns and 200,000 `prompt_utf8_v0_1` estimated tokens per UTC
day; this is a cost guard, not Provider billing parity. Install each secret from a controlled input
without placing its value in shell history, then enforce the D2 source contract:

```bash
sudo chown root:10001 /etc/aemeath/secrets/deepseek_api_key \
  /etc/aemeath/secrets/web_auth_csrf_key
sudo chmod 0440 /etc/aemeath/secrets/deepseek_api_key \
  /etc/aemeath/secrets/web_auth_csrf_key
```

Validate only structure; do not save or upload a full rendered Compose configuration:

```bash
sudo docker compose --env-file /etc/aemeath/prod.env config -q
sudo docker compose --env-file /etc/aemeath/prod.env config --services
sudo docker compose --env-file /etc/aemeath/prod.env config --profiles
```

The expected default service list is exactly `api` and `proxy`. The profile list may show
`memory`, `ops`, and `restore`, but this is not permission to run them during first-release
startup. D5 is not approved: `memory-worker` has no network, mount, or secret and exits
fail-closed. D6 `ops` and `restore` are one-shot backup/restore commands and must only be run
through the D6 runbook below. Never pass `--profile memory` in the first-release startup path.

Build the Web artifact before the proxy image:

```bash
cd web
npm ci
npm test
npm run build
cd ..
```

Start API first and allow its provider-free `/ready` healthcheck to pass before Caddy accepts
traffic; the dependency contract also enforces this ordering for a normal start:

```bash
sudo docker compose --env-file /etc/aemeath/prod.env up -d api
sudo docker compose --env-file /etc/aemeath/prod.env ps
sudo docker compose --env-file /etc/aemeath/prod.env up -d proxy
```

Check that only `aemeath-api-1` and `aemeath-proxy-1` exist, both are healthy, API has no published
port, and proxy alone publishes 80/443. Verify `/health`, `/ready`, Cookie authentication, SPA
fallback, security headers, direct rejection of synchronous `/v1/chat`, CORS, 128 KiB
Content-Length/chunked boundaries, and spoofed XFF through HTTPS without sending a real Provider
request. Caddy certificate/config state lives in `aemeath_caddy_data` and
`aemeath_caddy_config`; application SQLite/JSONL state lives only in the approved bind mounts.
Before accepting D4 in staging, verify the non-root Caddy process can write both named volumes.

For the D4 persistence drill, create a mock-only test session in an isolated staging deployment,
record non-sensitive identifiers and file metadata, then run `sudo docker compose restart`,
followed by `sudo docker compose down` and `sudo docker compose up -d`. Do not use `down -v`.
Confirm the session and JSONL state remain, the Caddy named volumes retain state, and no Memory
Worker was created. Host or
ECS reboot persistence and image replacement remain part of D8 staging acceptance.

The proxy healthcheck covers both the local Caddy admin endpoint and provider-free
`http://api:8000/health`. For the bounded fault drill, stop API with controlled sudo and allow up to
75 seconds for proxy to become unhealthy. This bound accounts for the 15-second interval, 3-second
probe timeout, four retries, and scheduling phase. Start API again and confirm API and proxy both
recover to healthy. A 502 after that bound while the proxy is still marked healthy is a failed drill.

## D1–D4 verification

Run without loading a local dotenv file:

```bash
AEMEATH_ENVIRONMENT=test python -m pytest -q -p no:cacheprovider --basetemp .pytest-tmp
AEMEATH_ENVIRONMENT=test python -m ruff check .
AEMEATH_ENVIRONMENT=test python -m compileall -q app scripts tests
python scripts/deployment/check_deploy_lock.py
sudo docker compose --env-file /etc/aemeath/prod.env config -q
git diff --check
```

For WSL, also build the image, verify UID/read-only startup and inspect that `.env`, DB/log files,
GPU libraries, compilers, pip, and setuptools are absent. Validate Caddy with the digest-pinned
image, run the default-service/no-Worker inspection, and complete the restart/down-up persistence
drill with mock credentials in isolated host roots. Do not send a real DeepSeek request without
separate Manager authorization.

## Deferred

D5 Memory Worker WAL/busy-timeout plus lease/stale-recovery gates, D7 retention/monitoring, and D8
ECS staging drills remain intentionally absent. The D4 real-container
restart/down-up and fault-injection drills passed Manager revalidation on WSL2 Ubuntu 22.04. Later
ECS reboot, image-replacement, and staging fault drills remain separate gates. T-0140 remains
pending.

## D6 SQLite Backup, Verify, Restore

D6 adds networkless `ops` and `restore` profile commands for SQLite backup and recovery. These
commands still use the controlled root operator contract: keep `/etc/aemeath/prod.env` root-only and
run production Compose through `sudo docker compose --env-file /etc/aemeath/prod.env ...`.
Production `sqlite_raw` backup refuses placeholder rollback identity: `AEMEATH_APP_IMAGE_DIGEST` and
`AEMEATH_OPS_IMAGE_DIGEST` must be exact `sha256:<64 hex>` digests, both deploy-lock and rendered
Compose config hashes must be SHA-256 hex, and `AEMEATH_BUILD_COMMIT` must be the producing Git
commit.

Nightly backup:

```bash
sudo docker compose --env-file /etc/aemeath/prod.env \
  --profile ops run --rm ops
```

Pre-upgrade backup should record the old immutable deployment identity:

```bash
sudo docker compose --env-file /etc/aemeath/prod.env \
  --profile ops run --rm ops \
  python /opt/aemeath/scripts/deployment/backup_sqlite.py \
  --db-path /var/lib/aemeath/data/aemeath.db \
  --backup-root /var/backups/aemeath \
  --backend sqlite_raw \
  --label pre-upgrade \
  --app-image-digest "$OLD_APP_IMAGE_DIGEST" \
  --ops-image-digest "$OLD_OPS_IMAGE_DIGEST" \
  --deploy-lock-sha256 "$OLD_DEPLOY_LOCK_SHA256" \
  --compose-config-sha256 "$OLD_COMPOSE_CONFIG_SHA256" \
  --git-commit "$OLD_BUILD_COMMIT"
```

Verify a backup:

```bash
sudo docker compose --env-file /etc/aemeath/prod.env \
  --profile ops run --rm ops \
  python /opt/aemeath/scripts/deployment/verify_sqlite_backup.py \
  --backup-dir /var/backups/aemeath/backups/YYYY/MM/DD/aemeath-...-bkp_... \
  --backend sqlite_raw
```

Restore requires stopped writers and explicit confirmation:

```bash
sudo docker compose --env-file /etc/aemeath/prod.env stop api
sudo docker compose --env-file /etc/aemeath/prod.env ps
sudo docker compose --env-file /etc/aemeath/prod.env \
  --profile restore run --rm restore \
  python /opt/aemeath/scripts/deployment/restore_sqlite.py \
  --data-root /var/lib/aemeath/data \
  --db-name aemeath.db \
  --backup-dir /var/backups/aemeath/backups/YYYY/MM/DD/aemeath-...-bkp_... \
  --journal-root /var/lib/aemeath/data/.restore-journal \
  --expected-backend sqlite_raw \
  --confirm-writers-stopped
sudo docker compose --env-file /etc/aemeath/prod.env up -d api
sudo docker compose --env-file /etc/aemeath/prod.env up -d proxy
```

The backup artifact is one generation directory containing `aemeath.db` and immutable
`manifest.json`. D6 never copies a live DB file directly and never copies live `-wal`/`-shm`
sidecars into the backup artifact.

The restore journal treats `aemeath.db`, `aemeath.db-wal`, and `aemeath.db-shm` as one old set. Each
file move has a journaled intent, digest, location, and completion state. After a crash,
`active.pointer.json` is only a locator/progress hint; the implementation reconciles the verified
per-restore journal with file locations and digests before continuing.
The restore command also holds an OS-owned `active.lock` for the whole mutation path; lock file
existence is not authoritative, and a competing restore fails with `restore_in_progress`.

For upgrade-paired rollback, use the ops image and config recorded in the backup manifest to verify
and restore the pre-upgrade backup, then start the matching old API/proxy pair. A newer image may
correctly reject an older schema contract.

D6 does not implement off-ECS transfer, backup pruning, or quarantine pruning.
