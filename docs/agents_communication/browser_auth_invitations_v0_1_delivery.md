# Worker Session 18：Browser Auth And Invitations v0.1 A7 Delivery Report

Date: 2026-08-04

Status: **Changes Requested repairs implemented; Worker verification complete; awaiting Manager re-review**

Design authority: `docs/agents_communication/browser_auth_invitations_v0_1_design.md`

## 1. Delivery conclusion

Browser Auth And Invitations v0.1 is implemented against the accepted contract. The delivery adds
explicit `dev_bearer|web_session` adapters, invitation and operator-assisted reset flows, Argon2id
credentials, opaque server-side Web Sessions, deterministic per-Session CSRF, startup-validated
SQLite migrations, and the seven frozen Auth endpoints.

The accepted D4 Bearer baseline remains the production default. This delivery did not modify
`compose.yaml`, Caddy/deployment topology, `frontend/`, `DialogueController`, or D6 backup/restore.
The Web profile is implemented but is not activated as the D4 production profile.

### Manager re-review repair revision

The three findings in
`docs/agents_communication/browser_auth_invitations_v0_1_manager_review.md` are addressed narrowly:

1. Schema validation now uses quote-aware SQL tokenization that preserves single-quoted literal
   bytes. It compares the complete canonical trigger bodies and complete named-index definitions,
   including exact partial predicates. Auth columns, nullability/defaults, primary keys, CHECK
   expressions, token uniqueness, and foreign keys are also structurally checked. The schema
   fingerprint uses the same literal-preserving canonical form. Raw SQLite and
   SQLAlchemy-on-SQLite restart tests reject narrowed/expanded predicates, `AND 0`, literal
   case/space drift, and changed trigger conditions, while proving that each mutation changes the
   fingerprint.
2. The Web provenance middleware now performs global/IP pre-auth admission for login, invitation
   accept, and reset accept before FastAPI reads or validates the request body. Login applies only
   the identity bucket after bounded DTO parsing, so global/IP capacity is not consumed twice.
   Malformed JSON and DTO-validation regressions prove early capacity consumption and zero password
   work.
3. `BrowserSessionAuthService`, `InvitationService`, and `PasswordResetService` now depend on the
   narrow protocols in `app/auth/ports.py`. The protocols cover every service UoW operation; only
   the API/CLI composition roots select the concrete repository. A static architecture test rejects
   future `app.db` imports from Auth domain modules (the operator CLI composition root is explicit).

## 2. Implemented surface

### A1 — Password, token, CSRF, and rate primitives

- Added `argon2-cffi>=25.1,<26` and pinned Linux deployment artifacts.
- Frozen `argon2id_owasp_2026_v1`: `m=19456 KiB,t=2,p=1,hash_len=32,salt_len=16`.
- Password input is exact, untrimmed, 12–128 printable Unicode code points, strict UTF-8.
- Password work uses one bounded semaphore gate; login has a fixed dummy Argon2 verification path.
- Invitation/reset/session tokens use 256-bit CSPRNG values, strict type prefixes, and domain-separated
  SHA-256 digests. Raw values are repr-hidden and are never persisted.
- CSRF is derived with HMAC-SHA256 from the fixed secret, Web Session internal id, and a 32-byte
  per-Session verifier. The database does not store the client CSRF token.
- Added a bounded HMAC-pseudonymized pre-auth limiter with global, IP, and login-identity buckets.

Primary files:

- `app/auth/passwords.py`
- `app/auth/tokens.py`
- `app/auth/csrf.py`
- `app/auth/rate_limit.py`
- `app/auth/schemas.py`
- `app/auth/ports.py`
- `app/auth/qualify_passwords.py`

### A2 — Schema, migration, and Store parity

- Added permanent `accounts.username_normalized TEXT NOT NULL DEFAULT ''`.
- Added the frozen unique index and three strict insert/update/immutability triggers.
- Added `account_credentials`, `account_invitations`, `account_password_resets`, and
  `web_auth_sessions`, including lifecycle, version, verifier-length, FK, and unique constraints.
- `web_auth_sessions.authenticated_credential_version` has `CHECK >= 1`.
- Both raw SQLite and SQLAlchemy-on-SQLite use the explicit `browser_auth_v0_1` migrator and the same
  transactional Auth repository behavior.
- Migration precomputes legacy normalization collisions before durable writes, uses
  `BEGIN IMMEDIATE`, validates the sentinel, writes the marker last, and rolls back on failure.
- Marker-present startup still introspects columns/defaults, all named triggers, CHECK guards,
  foreign keys, and required index uniqueness/column order/partial predicates.
- Migration and every startup run whole-database `PRAGMA foreign_key_check` and fail closed on any row.
- Every raw/SQLAlchemy Auth connection enables and verifies `PRAGMA foreign_keys=ON`.
- Added read-only `StoreSchemaInfo` with backend migration ids, Auth revision, and a content-free
  schema fingerprint. No `user_version`, `application_id`, or D6 implementation was added.

Primary files:

- `app/db/browser_auth_migrations.py`
- `app/db/repositories/browser_auth.py`
- `app/db/models.py`
- `app/db/schema.sql`
- `app/db/session.py`
- `app/db/sqlite_store.py`
- `app/db/sqlalchemy_store.py`
- `app/accounts/schemas.py`
- `app/db/repositories/accounts.py`

### A3/A4 — Services and operator CLI

- Invitation reissue atomically revokes old pending/derived-expired invitations before creating the
  new row. Deterministic acceptance identity conflicts atomically revoke the invitation.
- Invitation and reset acceptance order is rate gate, strict token/digest preflight, password policy,
  Argon2, then transaction revalidation/consumption. Unknown/expired/revoked/version-mismatched
  candidates do not enter Argon2.
- Login performs dummy work for unavailable, credentialless, disabled, and Browser-ineligible legacy
  identities. Session creation rechecks active status, exact credential hash, and version in the write
  transaction; representation-only rehash keeps the version.
- Every Session resolve joins the current credential and compares its version. Expired, disabled,
  missing-credential, revoked, and version-mismatched Sessions fail closed and are revoked idempotently.
- Password change uses expected hash/version CAS, increments the credential version, revokes all old
  Sessions, and creates one replacement Session in the same transaction.
- Reset accept supports the approved version-0 credentialless legacy enrollment and approved automatic
  login; ordinary reset increments the version and rotates all Sessions.
- Added touch CAS, current/all revoke, account disable/enable, and bounded Session cleanup.
- Added narrow `python -m app.auth.admin` commands for invite issue/list/revoke, reset issue,
  disable/enable, revoke-account-sessions, and cleanup. It does not construct model, embedding, memory,
  prompt, or Controller runtime. Raw operator URLs are printed once, after commit.

Primary files:

- `app/auth/browser_sessions.py`
- `app/auth/invitations.py`
- `app/auth/admin.py`

### A5 — FastAPI adapter

Endpoints:

- `POST /v1/auth/invitations/accept` → 201
- `POST /v1/auth/login` → 200
- `POST /v1/auth/logout` → 204
- `GET /v1/auth/me` → 200
- `GET /v1/auth/csrf` → 200
- `POST /v1/auth/password/change` → 204
- `POST /v1/auth/password-reset/accept` → 200 and fresh login Session

The adapter enforces:

- explicit non-hybrid auth mode and Bearer/Cookie conflict rejection;
- exact JSON media type, configured Origin or Referer origin, and Fetch Metadata for all unsafe
  Web-mode `/v1/*` requests;
- CSRF on existing Chat and Session unsafe routes as well as Auth Session mutations;
- exactly one bounded `__Host-aemeath_session` Cookie;
- `Secure; HttpOnly; SameSite=Strict; Path=/`, no Domain, and no persistent expiry on set;
- rotation on login/invite/reset/change and exact-attribute clearing on logout/invalid Session;
- `Cache-Control: no-store` on every Auth success and error;
- `SecretStr` request fields and validation responses that do not echo input.

Web Session internal ids stay in Auth request state and repr-hidden DTO fields. Business
`UserContext.metadata` contains only `auth_method=web_session`. App request logs use only the frozen
irreversible `web_session_correlation`; logging is best-effort and cannot change an Auth commit result.

Primary files:

- `app/api/routes/auth.py`
- `app/api/web_security.py`
- `app/api/dependencies.py`
- `app/api/schemas.py`
- `app/api/errors.py`
- `app/api/server.py`
- `app/config.py`
- `app/deployment/secrets.py`
- `app/errors/codes.py`
- `app/errors/http.py`
- `app/errors/sanitizer.py`

## 3. New verification coverage

The six new suites contribute 63 collected cases in the current Windows run, including parameterized execution
against both Store backends:

- `tests/test_password_credentials.py`
- `tests/test_browser_auth_migrations.py`
- `tests/store_contract/test_browser_auth_contract.py`
- `tests/test_browser_auth.py`
- `tests/test_api_auth.py`
- `tests/test_auth_admin_cli.py`

Coverage includes token domain separation, Argon profile/gate, CSRF derivation, early pre-auth
admission/rate capacity, legacy migration/collision/structural-drift/orphan failure, Store parity,
concurrent invite/reset single-use, credential
CAS/version invalidation, reset enrollment, rotation, cleanup, dummy login, zero internal-id logging,
all seven endpoints, HTTPS Secure Cookie behavior, provenance/content/CSRF matrices, wrong-mode/dual
credentials, protocol dependency direction, generic masking, and one-time CLI token output.

## 4. Fresh verification evidence

### Windows primary run

Environment: Windows, Python 3.13.13, pytest 9.0.3, SQLite 3.50.4.

```text
python -m pytest tests/test_password_credentials.py tests/test_browser_auth_migrations.py \
  tests/store_contract/test_browser_auth_contract.py tests/test_browser_auth.py \
  tests/test_api_auth.py tests/test_auth_admin_cli.py -q \
  --basetemp=.tmp/pytest-auth-focused-final -p no:cacheprovider
63 passed, 1 warning

python -m pytest -o addopts='' -ra \
  --basetemp .tmp/pytest-full-manager-repair -p no:cacheprovider
657 passed, 1 skipped, 35 warnings in 43.08s

python -m ruff check . --exclude frontend
All checks passed!

python -m compileall -q app tests
passed

git diff --check
passed

python scripts/deployment/check_deploy_lock.py
status=ok; no missing, unhashed, unpinned, or constraint-mismatched packages
```

The one skipped test is the pre-existing pinned Caddy body-limit integration, which requires
`AEMEATH_TEST_CADDY_BINARY`.

### Python 3.10 Linux/x86_64

The following is retained pre-repair A1–A6 qualification evidence. The narrow Manager repairs were
rerun on the Windows primary environment above; they were not rerun in WSL. Environment: WSL Ubuntu
22.04, Python 3.10.12, x86_64.

```text
deployment lock install with --only-binary=:all: --require-hashes
passed

installed versions
argon2-cffi=25.1.0; fastapi=0.140.13; sqlalchemy=2.0.51

Browser Auth focused suites
46 passed, 1 warning in 7.82s

python3 -m compileall -q app tests
passed
```

The unfiltered Linux full run collected 641 items and produced `638 passed, 1 skipped, 2 failed`.
Both failures are pre-existing environment/platform cases and pass in the Windows primary run:

1. `test_docker_compose_cli_accepts_only_safe_queries`: WSL exposes the Docker Desktop CLI stub, so
   `shutil.which('docker')` passes, but Docker Desktop integration/engine is unavailable.
2. `test_prompt_log_inspector_selects_latest_records`: `/mnt/c` mtime ordering differs from the test's
   NTFS expectation. This is unrelated to Auth and passed on Windows.

A diagnostic rerun excluding exactly those two named cases produced:

```text
638 passed, 1 skipped, 2 deselected, 17 warnings in 53.05s
```

### Argon2 single-CPU qualification

Command ran in the locked Python 3.10 Linux environment under `taskset -c 0` with
`--require-single-cpu`:

```text
samples=11
cpu_affinity_count=1
concurrency=1
hash p50=0.016370s, p95=0.019017s, p99=0.019017s
verify p50=0.015864s, p95=0.018273s, p99=0.018273s
peak_rss_bytes=78172160
p95_limit_seconds=1.0
passed=true
```

No password, hash, salt, token, or verifier was emitted by qualification.

## 5. Preserved boundaries

- D4: `dev_bearer` remains the default and the only accepted current production mode; existing Bearer
  tests pass. `compose.yaml`, Caddy files, and D4 topology were not modified.
- `frontend/`: not modified or used by the implementation; no frontend content review was performed.
  Frontend/E2E activation remains later work.
- `DialogueController`: not modified or imported by Auth dependencies/routes/services.
- D6: backup/restore was not modified; only the approved read-only `StoreSchemaInfo` was added.
- Existing dialogue Session/Message/Memory/relationship ownership remains downstream of `UserContext`.
- Manager-owned shared docs/status were not modified by this Worker delivery. Pre-existing working-tree
  changes in those files remain unclaimed.

## 6. Outstanding checks and residual risks

- A real browser against a Caddy HTTPS Web profile was not run. Secure Cookie auto-send, rotation,
  Origin, CSRF, and current Chat/Session protection were verified with HTTPS FastAPI `TestClient`.
- The Web production profile remains intentionally inactive until frontend/E2E work. Its eventual
  Caddy/environment wiring and root-owned CSRF `_FILE` permissions require that later deployment review.
- Root-level `python -m ruff check .` still reports one pre-existing import-order issue in
  `frontend/capture_frontend.py`. The frozen scope forbids modifying `frontend/`; the implementation
  scope (`ruff check . --exclude frontend`) is clean.
- Rate limiting and password gates are process-local. The current accepted single-worker D4 boundary
  must remain until a distributed control is separately approved.
- The pinned Caddy binary integration remains skipped without its explicit test binary.
- `list-invites` cursor pagination is explicitly deferred for the invitation-only small launch. The
  current operation remains bounded to `1..200` records; cursor pagination is required as a separate
  follow-up before scaled operator use.
- Both current facades intentionally use the same SQLite-specific Browser Auth repository for the
  frozen SQLite launch. A future PostgreSQL implementation must implement the Auth ports and must not
  inherit this adapter assumption.

## 7. Handoff

A1–A6 and the three requested repairs are complete. No claim is made that T-0142 is `done`; it remains
`in_progress` until Manager re-review and final acceptance.
