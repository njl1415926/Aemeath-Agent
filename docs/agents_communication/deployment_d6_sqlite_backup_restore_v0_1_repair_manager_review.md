# Deployment D6 SQLite Backup/Restore v0.1 Repair Manager Review

- Date: 2026-08-07
- Reviewer: Manager Session
- Status: Changes Requested After Repair
- Scope: Re-review of the four implementation findings from the first D6 Manager review

## Accepted Repairs

The following repairs are valid and remain accepted:

- restore mutation is protected by one OS-owned `active.lock`; Windows contention returned the stable
  `restore_in_progress` reason without creating a losing journal;
- candidate installation now records intent, prepares a same-directory temporary file, publishes with
  atomic replace, and reconciles a digest-matching live candidate after the tested crash windows;
- production `sqlite_raw` backup and manifest verification reject missing or malformed deployment
  identity values;
- deadline checks now cover post-copy integrity/FK checks, fsync, manifest construction, and all work
  before the generation-directory rename commit point;
- focused D6/Compose tests passed on Windows (`33 passed, 2 skipped`), and Ruff, compileall, and deploy
  lock validation passed.

The repair is not yet accepted because the following issues were found during Manager re-review.

## Findings

### High 1: Inactive D6 profile variables break the default D4 startup contract

`compose.yaml` places five `${VAR:?}` expressions in the inactive `ops` profile. Docker Compose
interpolates the complete document before profile selection, so `docker compose config` and the normal
API/proxy startup fail when D6 backup identity is absent, even though `ops` was not requested.

Manager reproduced the failure with the existing D4 production example and no D6 identity variables:

```text
error while interpolating services.ops.command.[]: required variable
AEMEATH_APP_IMAGE_DIGEST is missing a value
```

The identity variables are also absent from `deploy/env/prod.env.example`.

Required repair:

- preserve the D4 invariant that default `config` and `up -d` do not require inactive profile inputs;
- let the backup CLI remain the fail-closed enforcement point when `ops` is actually invoked;
- use non-required interpolation with an invalid placeholder, or let the CLI read the service env file,
  then reject missing identity before opening the source DB or creating a backup generation;
- add a Compose contract test proving default config succeeds without D6 identity and an ops/CLI test
  proving an attempted backup without identity exits `2` without publishing an artifact;
- document the identity values in the production env example without making them secrets.

### High 2: Explicit rollback can remove an untouched live database

`--rollback-restore-id` can call `_rollback_restore()` from `started`, `prepared`, or a partially moved
old-set stage. `_rollback_restore()` treats any live DB not already marked `rollback_completed` as a
failed candidate and moves it to `failed_candidate` before proving that a complete quarantined old set
exists.

Manager reproduced this from a durable `prepared` journal. The rollback command moved the original,
still-valid `aemeath.db` into `failed_candidate`, then failed with `sqlite_open_failed`; the live DB path
was left absent.

Required repair:

- freeze an explicit rollback stage matrix before any filesystem mutation;
- for `started`/`prepared`, either reject rollback with a stable reason while leaving the live set
  byte-for-byte unchanged, or implement a separately journaled safe abort contract;
- for `old_quarantine_in_progress`, reconcile every old-set file and either support a complete reverse
  movement protocol or reject rollback without moving anything; never treat proven old live files as a
  failed candidate;
- permit candidate-removal rollback only after the journal proves candidate publication may have begun
  and the old-set inventory is complete;
- add fault tests for explicit rollback from every nonterminal stage, including a byte-for-byte assertion
  on the live main/WAL/SHM set after every rejected operation.

### Medium 3: Parent-directory fsync failure after commit is reported ambiguously

The generation-directory rename is documented as the backup commit point. The implementation performs
two directory fsync calls after that rename inside the generic publication `try` block. If either fsync
fails, the CLI reports `backup_publish_failed` even though the complete generation is already visible.

Manager injected a post-rename directory-fsync failure and observed both:

- `D6SqliteError("backup_publish_failed")`; and
- a published generation containing `aemeath.db` and `manifest.json`.

Required repair:

- distinguish pre-commit publication failure from post-commit durability uncertainty;
- after the rename, never return a generic result that leaves the operator unable to tell whether a
  generation was committed;
- use a stable post-commit reason/result that includes safe `backup_id`/generation identity and requires
  independent verify before retention or retry decisions, or provide an equivalent unambiguous contract;
- add a fault-injection test after the commit rename and after each parent fsync.

## Environment Gates

- Ubuntu 22.04 WSL is available with Python 3.10, but it currently has no `pip`/`pytest`; Manager could
  not independently run the Linux-only two-process test in this review.
- Docker Desktop's Linux daemon remains unavailable, so the isolated Compose staging backup/restore
  drill remains pending.
- These gates are separate from the code findings above and remain required after the narrow repairs.

## Verification Performed

- Focused D6/Compose tests: `33 passed, 2 skipped`.
- Ruff focused deployment scope: passed.
- `python -m compileall -q app scripts tests`: passed.
- Deployment lock checker: passed with no missing, unpinned, unhashed, or incompatible packages.
- Manager fault probes reproduced the default Compose interpolation failure, unsafe early-stage
  rollback, and ambiguous post-commit fsync failure.

## Decision

D6 remains `Changes Requested`; T-0140 remains `pending`. Keep the four accepted repairs, fix only the
three findings above, then return for Manager re-review and Linux/Compose environment validation. Do not
use the current restore CLI against the real project database.

## Third Review After Narrow Repair

- Date: 2026-08-07
- Status: Runtime repairs accepted; release asset change requested; environment gates pending

The three runtime/integration findings above are repaired and accepted:

- inactive D6 profile values now use an invalid fallback, so default Compose configuration succeeds
  while an actual backup still fails closed in the CLI before opening the source DB;
- explicit rollback now rejects `started`, `prepared`, and `old_quarantine_in_progress` without changing
  the current main/WAL/SHM set, while `old_quarantined` rollback restores the proven old set;
- post-commit fsync failure now returns `backup_committed_durability_uncertain` / exit `24` with the safe
  backup identity and requires independent verification.

Manager focused validation passed with `42 passed, 2 skipped`; Ruff, compileall, and deployment lock
validation also passed.

### Release Blocker: Production env example is ignored and untracked

`deploy/env/prod.env.example` contains the new D6 identity documentation, but the entire `deploy/env/`
directory is ignored by the repository's generic `env/` rule. `git ls-files` confirms the example is not
tracked, while `git check-ignore` confirms `.gitignore` excludes it.

Consequences:

- a clean clone, GitHub checkout, and server checkout do not contain the documented production template;
- `docs/deployment.md` tells the operator to install a file that is absent from the repository;
- the Compose contract test references a local-only ignored file and can fail in a clean environment
  with Docker CLI available.

Required narrow repair:

- move the non-secret template to a tracked path such as `deploy/prod.env.example` and update references;
  or add a precise `.gitignore` exception that tracks only `deploy/env/prod.env.example` while continuing
  to ignore all real env files in that directory;
- verify the template contains placeholders/non-secret defaults only;
- confirm `git ls-files <template>` returns the template and focused tests pass from the tracked layout.

After this packaging repair, D6 code may be accepted as implementation-complete, but T-0140 must remain
`pending` until the Ubuntu Linux process test and isolated Docker Compose staging drill are executed.

## Final Code And Release Asset Review

- Date: 2026-08-08
- Status: D6 implementation accepted; environment acceptance pending

The production template has moved to `deploy/prod.env.example`, which is not ignored and is visible as
an untracked release asset ready to be included in the next commit. Deployment documentation and Compose
contract tests now use the tracked-layout path. The template contains non-secret values, empty D6
identity placeholders, and secret file paths only; it contains no Provider credential value.

Manager verification:

- focused D6/Compose suite: `42 passed, 2 skipped`;
- Ruff focused deployment scope: passed;
- `python -m compileall -q app scripts tests`: passed;
- deployment lock validation: passed;
- the new template is not matched by `.gitignore`;
- active deployment runbook and Compose contract tests use `deploy/prod.env.example`; historical review
  and delivery records retain the former path as audit evidence.

D6 code and release assets are accepted as implementation-complete. The next Git commit must include
`deploy/prod.env.example`; leaving it untracked would invalidate this acceptance. T-0140 remains
`pending` until the Linux-only two-process restore test and isolated Docker Compose staging drill pass
Manager review. The real project database remains outside test scope.
