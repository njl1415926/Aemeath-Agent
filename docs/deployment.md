# Deployment v0.1 — D1–D4 operator contract

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
  are disabled, and static Bearer auth is limited to the approved closed single-user test.

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

Start from `deploy/env/prod.env.example`, copy it outside the repository, and set the approved
non-default user ID and exact Caddy container IP. Do not pass that example directly while required
fields are blank. Production rejects any drift from the frozen API-only profile. Only dev may load
dotenv, and dotenv is not allowed to switch the initially selected environment to test or prod.

Secrets are file mounts, not environment values:

| Variable | Required container file | Purpose |
|---|---|---|
| `DEEPSEEK_API_KEY_FILE` | `/run/secrets/deepseek_api_key` | DeepSeek chat API |
| `AEMEATH_DEV_AUTH_TOKEN_FILE` | `/run/secrets/api_bearer_token` | closed-test Bearer token |

`_FILE` is an Aemeath application convention; Docker Compose does not automatically translate it.
The resolver rejects plaintext plus `_FILE`, production plaintext, any path other than the two
fixed paths above, legacy `OPENAI_API_KEY`, empty or oversized content, invalid UTF-8, NUL, and
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

`deploy/Caddyfile` overwrites inbound `X-Forwarded-*`, applies `request_body max_size 128KiB`
before proxying, and has no access-log directive. The D3 Caddy image reference is pinned in
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
`deploy/Caddy.Dockerfile.dockerignore` excludes the entire build context because the derived image
does not copy repository files.

Create production host paths before the first start:

```bash
sudo install -d -o 10001 -g 10001 -m 0750 \
  /var/lib/aemeath/data /var/lib/aemeath/rag_indexes \
  /var/log/aemeath /var/log/aemeath/app \
  /var/log/aemeath/trace /var/log/aemeath/prompt
sudo install -d -o root -g root -m 0750 /etc/aemeath /etc/aemeath/secrets
sudo install -d -o root -g root -m 0750 /var/backups/aemeath
sudo install -o root -g root -m 0640 deploy/env/prod.env.example /etc/aemeath/prod.env
```

The first-release operator contract is controlled root execution: keep the environment file
root:root `0640`, keep `/etc/aemeath` root-traversable only, edit the non-secret configuration with
`sudoedit /etc/aemeath/prod.env`, and run every production Compose command through `sudo`. Do not
make the environment file or Secret paths world-readable and do not treat membership in the Docker
group as permission to bypass this contract.

Set `AEMEATH_DOMAIN`,
`AEMEATH_DEFAULT_USER_ID`, `AEMEATH_DEV_AUTH_USER_ID`, and an immutable `AEMEATH_IMAGE` tag or
digest. The two user IDs must be the same approved non-default value. Install each secret from a
controlled input without placing its value in shell history, then enforce the D2 source contract:

```bash
sudo chown root:10001 /etc/aemeath/secrets/deepseek_api_key \
  /etc/aemeath/secrets/api_bearer_token
sudo chmod 0440 /etc/aemeath/secrets/deepseek_api_key \
  /etc/aemeath/secrets/api_bearer_token
```

Validate only structure; do not save or upload a full rendered Compose configuration:

```bash
sudo docker compose --env-file /etc/aemeath/prod.env config -q
sudo docker compose --env-file /etc/aemeath/prod.env config --services
sudo docker compose --env-file /etc/aemeath/prod.env config --profiles
```

The expected default service list is exactly `api` and `proxy`. The profile list may show
`memory`, `ops`, and `restore`, but this is not permission to run them. D5 is not approved:
`memory-worker` has no network, mount, or secret and exits fail-closed. D6 is not implemented:
the `ops` and `restore` one-shot placeholders also exit fail-closed. Never pass `--profile memory`
in the first-release startup path.

Start API first and allow its provider-free `/ready` healthcheck to pass before Caddy accepts
traffic; the dependency contract also enforces this ordering for a normal start:

```bash
sudo docker compose --env-file /etc/aemeath/prod.env up -d api
sudo docker compose --env-file /etc/aemeath/prod.env ps
sudo docker compose --env-file /etc/aemeath/prod.env up -d proxy
```

Check that only `aemeath-api-1` and `aemeath-proxy-1` exist, both are healthy, API has no published
port, and proxy alone publishes 80/443. Verify `/health`, `/ready`, authentication, CORS, 128 KiB
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

D5 Memory Worker WAL/busy-timeout plus lease/stale-recovery gates, D6 backup/restore, D7
retention/monitoring, and D8 ECS staging drills remain intentionally absent. The D4 real-container
restart/down-up and fault-injection drills passed Manager revalidation on WSL2 Ubuntu 22.04. Later
ECS reboot, image-replacement, and staging fault drills remain separate gates. T-0140 remains
pending.
