# T-0142 Browser Auth And Invitations v0.1 Manager Review

Date: 2026-08-04

Status: **Accepted / Done**

## Manager Re-review Acceptance

The three required changes below have been repaired and independently re-verified:

- schema drift validation now performs quote-aware, complete structural comparison for the
  canonical trigger and partial-index contracts; semantic DDL mutations fail closed for both
  storage backends;
- global/IP pre-auth admission now runs before request-body parsing, while the login identity
  bucket remains a separate second stage and does not double-charge request capacity;
- Browser Auth domain services now depend on complete narrow persistence protocols, with concrete
  SQLite repository construction confined to composition roots and a static dependency test.

The bounded `list-invites` cursor and a future PostgreSQL Auth adapter are explicitly deferred in
the delivery report. They do not block the invitation-only, SQLite-backed small launch.

Final Manager verification:

- Browser Auth focused suites: `63 passed`.
- Full Windows suite: `657 passed, 1 skipped` (`658` collected).
- `python -m ruff check . --exclude frontend`: passed.
- `python -m compileall -q app tests`: passed.
- `python scripts/deployment/check_deploy_lock.py`: passed.
- `git diff --check`: passed, with only existing line-ending warnings.
- The remaining skip is the existing optional Caddy-binary integration test.

No new blocking or non-blocking findings were found. T-0142 is accepted and may be marked `done`.
Production Web-profile activation, real-browser/Caddy acceptance, streaming, frontend work, and D6
backup/restore remain separate gated tasks.

## Initial Verification

- Browser Auth focused suites: `46 passed`.
- Full Windows suite: `640 passed, 1 skipped` (`641` collected).
- `python -m ruff check . --exclude frontend`: passed.
- `python -m compileall -q app tests`: passed.
- `python scripts/deployment/check_deploy_lock.py`: passed.
- `git diff --check`: passed, with only existing line-ending warnings.
- No real Caddy/browser Web profile or real provider call was performed in this review.

The findings below are retained as the initial-review record. They are resolved by the accepted
repair revision documented above.

## Resolved Required Changes

### 1. Strict schema drift gate is not strict

`app/db/browser_auth_migrations.py` normalizes SQL by lowercasing all text and validates trigger and
partial-index DDL with substring checks. Independent reproduction confirmed that startup accepts both:

- `uq_account_invitations_pending_username ... WHERE status = 'pending' AND 0`;
- `trg_accounts_username_identity_immutable` with its required condition followed by `AND 0`.

Both objects are behaviorally broken but pass `ensure_browser_auth_schema(...)`. This violates the
accepted fail-closed startup/fingerprint contract.

Required fix:

- use quote-aware structural normalization that preserves single-quoted literal bytes;
- validate the complete canonical trigger body and exact partial-index predicate, not fragments;
- ensure the schema fingerprint changes for semantic literal/predicate drift;
- add raw SQLite and SQLAlchemy-on-SQLite restart tests for narrowed/expanded predicates, `AND 0`,
  changed string-literal case/space, and altered trigger conditions.

### 2. Malformed pre-auth requests bypass the limiter

The login/invitation/reset routes call `_check_pre_auth(...)` inside the route function after FastAPI
has parsed and validated the Pydantic body. Independent reproduction with burst `1` showed that a
malformed JSON login request returned `INVALID_INPUT` without consuming pre-auth capacity; the next
validly shaped request was still admitted.

Required fix:

- enforce global/IP pre-auth admission before request-body parsing;
- preserve the login identity bucket after bounded DTO parsing without double-consuming global/IP
  capacity;
- add a regression proving malformed JSON and DTO-validation failures are covered by the early
  admission gate and never reach password work.

### 3. Auth domain services bypass the declared persistence port

`BrowserSessionAuthService`, `InvitationService`, and `PasswordResetService` import and type against
`app.db.repositories.browser_auth.BrowserAuthRepository`. Meanwhile `BrowserAuthStorePort` is unused
and does not expose the full UoW surface the services require. This breaks the project rule that
business modules depend on protocols rather than concrete storage adapters.

Required fix:

- complete or split narrow Auth store/UoW protocols for the service operations;
- type Auth services against those protocols and remove `app.db.*` imports from the Auth domain;
- keep concrete repository selection in the composition root;
- add a small architectural/static test that protects this dependency direction.

## Non-blocking Follow-up

- The accepted operator contract describes cursor pagination for `list-invites`, while the current
  repository/CLI only expose a bounded `limit`. For the invitation-only small launch this may be
  deferred, but the deferral must be recorded explicitly before final acceptance.
- The SQLAlchemy facade currently delegates Browser Auth UoWs to the same SQLite-specific repository
  as the raw backend. This is acceptable for the frozen SQLite launch if documented, but a future
  PostgreSQL adapter must implement the Auth ports rather than inherit this assumption.

## Initial Acceptance State

T-0142 remains `in_progress`. The Worker should make only the required narrow repairs, update the
delivery report, and rerun focused/full verification. Do not activate the production Web profile,
modify `frontend/`, change `DialogueController`, or enter D6 backup/restore work.
