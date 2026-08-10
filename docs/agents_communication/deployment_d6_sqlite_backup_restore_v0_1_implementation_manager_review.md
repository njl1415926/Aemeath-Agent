# Deployment D6 SQLite Backup/Restore v0.1 Implementation Manager Review

- Date: 2026-08-07
- Reviewer: Manager Session
- Status: Changes Requested
- Scope: D6 implementation, recovery safety, Compose boundary, and acceptance evidence

## Summary

The implementation has a useful foundation: online SQLite backup, independent read-only verification,
generation-directory publication, per-file old-set journaling, terminal pointer retirement, production
SQLAlchemy rejection, networkless Compose profiles, and the current focused regression all work.

D6 cannot be accepted yet. Manager fault injection reproduced three correctness gaps in the approved
recovery contract, and the production backup path does not currently guarantee the immutable deployment
identity required for paired rollback. The Docker Compose staging drill also remains an open environment
gate because the Docker daemon was unavailable during this review.

## Findings

### High 1: Restore has no inter-process exclusion

`restore_sqlite_backup()` reads the active pointer and creates journals without acquiring the approved
whole-operation `active.lock`. Two invocations can both observe no active pointer and begin different
restores against the same live database.

Manager reproduced this by pausing two restore calls after `journal_started`. Both passed the initial
pointer check; one completed and the other raced the live DB rename and raised a raw `FileNotFoundError`
instead of a stable `D6SqliteError` concurrency result.

Required repair:

- acquire a real inter-process lock before pointer/preflight/state inspection and hold it through
  verified/rolled-back pointer retirement;
- use OS-owned locking so process death releases ownership; do not use lock-file existence as the
  authoritative lock state;
- map lock contention to stable exit code `31` and a safe reason such as `restore_in_progress`;
- add a Linux multi-process test proving exactly one restore enters the mutation path and the other
  fails without changing the live DB or either journal.

### High 2: Crash after candidate publication cannot resume

`_install_candidate()` copies and fsyncs the candidate at the live DB path before durably advancing the
journal to `candidate_installed`. A crash in that interval leaves the authoritative journal at
`old_quarantined` while a valid candidate already exists at the live path.

Manager reproduced the approved `candidate_installed` fault point. Reentry failed with
`live_db_not_quarantined` instead of reconciling the live candidate and continuing verification or
rolling back.

Required repair:

- journal candidate-install intent before publication;
- publish through a same-directory temporary file plus fsync and atomic replace, or provide an
  equivalent crash-safe protocol;
- on reentry, reconcile prepared/temp/live candidate locations and digests before selecting continue
  or rollback;
- cover crashes before publication, after rename, after directory fsync, and before the durable stage
  update;
- include candidate-sidecar and rollback reentry tests so interrupted rollback cannot become an
  unclassified raw filesystem exception.

### High 3: Production backups can publish unusable rollback identity

The backup API and CLI default app image, ops image, deploy-lock hash, Compose-config hash, and commit to
`unknown`. The default Compose `ops` command supplies none of these values. A normal nightly backup can
therefore publish successfully without the exact old-image identity required by the approved paired
rollback contract.

Required repair:

- production `sqlite_raw` backup must reject missing, placeholder, or malformed immutable identity;
- image identities must use an exact digest form, and lock/config hashes must use validated SHA-256;
- the default Compose/operator path must supply these values deterministically rather than relying on
  undocumented ambient variables;
- manifest verification must validate the identity field types and formats;
- add tests proving the default production profile cannot publish an `unknown` identity and that the
  recorded old ops identity can be selected for the rollback drill.

### Medium 4: `max_duration_seconds` is not a total backup deadline

The deadline is checked during the SQLite backup callback and once immediately after the copy. It is not
checked around integrity/FK verification, fsync, schema/count collection, manifest creation, or before
the generation-directory commit.

Manager replaced integrity verification with an 80 ms delay and requested a 10 ms deadline. The
operation took about 107 ms and still published a generation.

Required repair:

- enforce the deadline through all pre-commit verification and publication preparation stages;
- define the generation-directory rename as the commit point and never report an ambiguous failure
  after a successfully committed rename;
- add deterministic clock/hook tests where verification or manifest work crosses the deadline and
  prove no generation is published.

### Medium 5: The approved recovery test matrix and staging gate are incomplete

Current focused tests pass, including the Windows suite and the separately reported WSL read-only WAL
probe. They do not yet cover the approved multi-process restore lock, candidate-install crash window,
total post-copy deadline, exact old-image rollback, complete fault injection, and permission/fsync
failure matrix.

The real Docker Compose staging drill is also still pending. This is an environment gate rather than a
code defect, but D6 cannot be accepted without it.

Required evidence after code repair:

- focused D6 and Compose contract tests;
- full project regression, Ruff, compileall, deploy-lock validation, and `git diff --check`;
- Ubuntu 22.04/Linux tests for read-only WAL and multi-process restore exclusion;
- isolated Docker Compose drill: API writes during online backup, independent verify, writers stopped,
  restore, API/proxy restart, `/health`, `/ready`, auth shape, data survival, and no Memory Worker;
- a D6 delivery report containing only safe IDs, hashes, timings, and command results.

## Verification Performed

- Focused D6/Compose tests: passed (`24 passed, 1 skipped` from the current focused selection).
- Ruff on `app`, `scripts`, and focused deployment tests: passed.
- `python -m compileall -q app scripts tests`: passed.
- `git diff --check`: no whitespace errors; existing CRLF normalization warnings only.
- Docker daemon: unavailable; Compose staging drill not executed by Manager.
- Ubuntu 22.04 WSL distribution exists but was stopped at inspection time.

## Decision

D6 remains `Changes Requested`; T-0140 remains `pending`. Do not run these restore tools against the
real `data/aemeath.db`. The Worker should make only the narrow repairs above and return for Manager
re-review before any D7/D8 acceptance claim.
