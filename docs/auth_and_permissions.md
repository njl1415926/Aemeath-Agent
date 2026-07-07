# Auth And Permissions v0.1

## Scope

Auth / Account v0.1 provides a local account model, default account bootstrap, optional dev/static
token parsing, and a minimal owner-only permission helper.

It does not implement username/password login, registration, cookies, refresh tokens, OAuth,
admin UI, shared sessions, complex RBAC, FastAPI, or Web UI.

## Runtime Boundary

```text
CLI / future API adapter
  -> AuthService / LocalDefaultAccountResolver
  -> UserContext
  -> AgentService
  -> DialogueController
```

`AgentService` trusts the supplied `UserContext`. `DialogueController` does not know about
accounts, tokens, cookies, login, or account status.

## Accounts

SQLite stores local accounts in `accounts`:

```text
id TEXT PRIMARY KEY
username TEXT NOT NULL UNIQUE
display_name TEXT
status TEXT NOT NULL
roles_json TEXT NOT NULL DEFAULT '["user"]'
dev_token_hash TEXT
created_at TEXT NOT NULL
updated_at TEXT NOT NULL
last_login_at TEXT
```

The default local account is:

```text
id=user_default
username=user_default
display_name=Default User
roles=["user"]
status=active
```

Startup/backfill is idempotent. Historical `user_id` values are collected from existing runtime
tables when available: `sessions`, `messages`, `relationship_states`, `long_term_memories`, and
`memory_extraction_jobs`.

## Dev Token Auth

Optional local dev-token configuration:

```env
AEMEATH_DEV_AUTH_TOKEN=
AEMEATH_DEV_AUTH_USER_ID=user_default
```

When configured, startup hashes the token and stores only `dev_token_hash` on the configured
account. The raw token must not be logged, persisted, included in exceptions, or returned in public
payloads.

`AuthService.resolve_bearer_token(token)` accepts a raw token or `Bearer ...` value and returns
`UserContext` for an active matching account.

## Permissions

Permission v0.1 is owner-only:

```python
PermissionService.require_owner(
    resource_user_id=resource_user_id,
    user_context=user_context,
)
```

Session ownership failures continue to use `SESSION_OWNERSHIP_DENIED`. Generic non-session
owner-only failures use `PERMISSION_DENIED`.

## Errors

New stable codes:

- `AUTH_REQUIRED` -> 401
- `AUTH_INVALID_CREDENTIALS` -> 401
- `AUTH_ACCOUNT_DISABLED` -> 403
- `PERMISSION_DENIED` -> 403

Public payloads must not reveal token values, account secrets, or resource ownership details.

## Deferred

- FastAPI
- Web UI
- username/password login
- password hashing
- registration/login pages
- OAuth
- refresh tokens
- cookie session
- admin UI
- complex RBAC
- shared sessions
- `DialogueController` auth changes
