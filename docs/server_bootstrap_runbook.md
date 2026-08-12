# Server Bootstrap Runbook

Status: verified core path for `v1.0.0` on Ubuntu 22.04 `linux/amd64`.

This is the single entry point for a fresh invitation-only deployment. Read `docs/deployment.md`
for the complete security, backup, and operations contracts. Never paste Secret values, invitation
URLs, passwords, or rendered Compose output into tickets or chat.

## B0. Host and Network

Use one Ubuntu 22.04 x86_64 host with Docker Engine and the Compose plugin. Open inbound TCP 80 and
443 publicly; restrict SSH 22 to operator addresses; never publish 8000. Point an A record such as
`chat.example.com` to the host public IPv4 before starting Caddy.

Verify:

```bash
uname -m
cat /etc/os-release
docker --version
docker compose version
python3 --version
free -h
df -h /
```

## B1. Clone the Release

```bash
git clone --branch release/v1.0 --single-branch \
  https://github.com/njl1415926/Aemeath-Agent.git /opt/aemeath
cd /opt/aemeath
git status --short
git log -1 --oneline
```

The checkout must be clean and the release commit must carry the `v1.0.0` tag.

## B2. Build the Web Artifact

Install Node.js 20 LTS and npm if they are absent. Node is a build-time dependency only.

```bash
cd /opt/aemeath/web
npm ci
npm test
npm run build
test -f dist/index.html
cd /opt/aemeath
```

Do not run `npm audit fix` during a release deployment.

## B3. Create Host State

```bash
install -d -o 10001 -g 10001 -m 0750 \
  /var/lib/aemeath/data /var/lib/aemeath/rag_indexes \
  /var/log/aemeath /var/log/aemeath/app \
  /var/log/aemeath/trace /var/log/aemeath/prompt
install -d -o root -g root -m 0750 \
  /etc/aemeath /etc/aemeath/secrets /var/backups/aemeath
install -o root -g root -m 0640 \
  deploy/prod.env.example /etc/aemeath/prod.env
```

## B4. Configure Production

Edit `/etc/aemeath/prod.env` with `sudoedit`. At minimum, set:

```env
AEMEATH_DOMAIN=chat.example.com
AEMEATH_WEB_PUBLIC_ORIGIN=https://chat.example.com
AEMEATH_DEFAULT_USER_ID=aemeath_bootstrap
AEMEATH_IMAGE=aemeath-agent:local
```

Keep the frozen defaults for Web Session auth, raw SQLite, Context Budget, App/Trace logs, and
quota. Keep long-term Memory, embedding, model fallback, mock LLM, and prompt-run logs disabled.

## B5. Install Secrets

Create these host files from controlled input without placing values in shell history:

```text
/etc/aemeath/secrets/deepseek_api_key
/etc/aemeath/secrets/web_auth_csrf_key
```

The CSRF Secret must contain at least 32 random bytes. Then apply:

```bash
chown root:10001 \
  /etc/aemeath/secrets/deepseek_api_key \
  /etc/aemeath/secrets/web_auth_csrf_key
chmod 0440 \
  /etc/aemeath/secrets/deepseek_api_key \
  /etc/aemeath/secrets/web_auth_csrf_key
stat -c '%n uid=%u gid=%g mode=%a bytes=%s' \
  /etc/aemeath/secrets/deepseek_api_key \
  /etc/aemeath/secrets/web_auth_csrf_key
```

The expected numeric ownership is `uid=0 gid=10001 mode=440`. A host display of
`group=UNKNOWN` is harmless when numeric GID 10001 is correct.

## B6. Validate and Build Images

```bash
cd /opt/aemeath
python3 scripts/deployment/check_deploy_lock.py
docker compose --env-file /etc/aemeath/prod.env config -q
docker compose --env-file /etc/aemeath/prod.env config --services

BUILD_COMMIT="$(git rev-parse HEAD)"
docker build --platform linux/amd64 \
  --build-arg BUILD_COMMIT="$BUILD_COMMIT" \
  --tag aemeath-agent:local .
docker build --platform linux/amd64 \
  --file deploy/Caddy.Dockerfile \
  --tag aemeath-caddy:2.10.2-nonroot .
```

Expected runtime users are `10001:10001` for API and `10002:10002` for Caddy.

## B7. Start and Verify

```bash
docker compose --env-file /etc/aemeath/prod.env up -d api
docker compose --env-file /etc/aemeath/prod.env ps
docker compose --env-file /etc/aemeath/prod.env up -d proxy
docker compose --env-file /etc/aemeath/prod.env ps
```

Only proxy may publish host ports 80/443. API should show only `8000/tcp`, never a host mapping.
Verify externally:

```bash
curl https://chat.example.com/health
curl https://chat.example.com/ready
```

If Caddy reports ACME connection timeout while local 80/443 are listening, fix the cloud security
group. Do not delete Caddy volumes or repeatedly restart the proxy. Follow logs only when needed:

```bash
docker compose --env-file /etc/aemeath/prod.env logs --tail 100 proxy
```

## B8. Create the First Invitation

```bash
docker compose --env-file /etc/aemeath/prod.env exec -T api \
  python -m app.auth.admin create-invite \
  --username admin \
  --display-name "Administrator"
```

The URL is a one-time credential. Open it privately, set a password, and sign in with `username`,
not `display_name`. Passwords are stored only as Argon2id hashes and cannot be viewed; use
`issue-password-reset --account <username>` when recovery is needed.

## B9. Acceptance and Operations

Before expanding the invitation group:

1. Test invitation, login, Session creation/lifecycle, streaming chat, refresh, logout, and mobile.
2. Complete one D6 backup, independent verify, and staging restore.
3. Verify persistence across container replacement and host reboot.
4. Install and validate the D7 backup, log-retention, and host-health timers.

Use `docs/deployment.md` for the exact D6/D7 commands and never run `docker compose down -v`.
