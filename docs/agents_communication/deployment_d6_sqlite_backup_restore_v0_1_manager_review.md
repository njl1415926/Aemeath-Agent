# Deployment D6 SQLite Backup/Restore v0.1 Manager Review

- Date: 2026-08-07
- Reviewer: Manager Session
- Reviewed document: `deployment_d6_sqlite_backup_restore_v0_1_design.md`
- Status: Manager Accepted After Revision; implementation authorized
- Scope: D6 design review and implementation authorization

## Summary

The overall direction is approved: use SQLite's online backup API, verify artifacts independently
and read-only, stop all writers before restore, isolate the old main/WAL/SHM set, retain rollback
material, and keep the D6 Compose profiles networkless and free of Provider secrets.

The first review blocked implementation until the restore journal could represent every filesystem
state, including crashes between individual renames, and until repeated restores and upgrade-paired
rollback had executable contracts. The revised design closes those findings and is authorized for
implementation under the acceptance conditions below.

## Manager Re-review Acceptance

The revised design was accepted on 2026-08-07:

- old main/WAL/SHM movement now uses per-file intent, completion, digest, and location reconciliation;
- each restore has an immutable journal directory and a retireable active pointer, allowing later
  restores without deleting retained quarantine;
- backup identity includes the exact ops/app image and deployment/schema contract, and paired
  rollback runs from the recorded old ops image;
- backup publication uses one generation-directory rename and the manifest remains immutable;
- backup execution has a total deadline and Linux read-only WAL acceptance coverage;
- all five Manager decisions are frozen in the design.

Implementation condition: `active.pointer.json` is a locator and progress hint, not the authoritative
state machine record. If a crash leaves pointer stage and `journal.json` stage unequal, the
implementation must validate identities, use the newest valid journal plus filesystem/digest
reconciliation as authority, and never execute a stale pointer stage. Add a fault-injection test for
the journal-written/pointer-not-yet-updated interval.

## Findings

### High 1: Partial old-set movement is not represented by the journal

The design moves `aemeath.db`, `aemeath.db-wal`, and `aemeath.db-shm` individually, but exposes only a
single `prepared -> old_quarantined` stage. A process or host crash after moving one file and before
moving the others leaves a split database set while the durable journal still says `prepared`.
Current reentry semantics assume the live set is unchanged at `prepared`, so automatic continuation
or rollback can combine the wrong main file and sidecars.

Required revision:

- Freeze an inventory before movement with each file's expected presence, size, digest, source path,
  quarantine path, and movement state.
- Journal and fsync an intent before every rename; fsync both source and destination directories;
  then journal and fsync the completed rename.
- On reentry, reconcile every file by digest and location. For each expected file, exactly one valid
  copy may exist in live or quarantine. Missing, duplicate, or mismatched copies fail closed.
- Do not use a single `old_quarantined` transition until all expected files have been reconciled in
  quarantine and no old sidecar remains at the live path.
- Add crash injection after every journal write, rename, and directory fsync for main, WAL, and SHM.

### High 2: A terminal `active.json` prevents a second restore

The current rules return `already restored` for `verified` and `already rolled back` for
`rolled_back`, but do not define how `active.json` is retired. After the first successful restore,
future restore requests would keep loading the old terminal journal and could never start a new
`restore_id` safely.

Required revision:

- Use one immutable journal directory per `restore_id` plus an atomic active pointer/record.
- Terminal finalization must archive the active journal to retained history and atomically clear the
  active pointer before another restore may start.
- A crash while retiring the pointer must be idempotently recoverable.
- Starting a new restore while a nonterminal active journal exists must fail closed unless the
  invocation explicitly resumes that exact `restore_id`.
- Quarantine remains retained even after the active pointer is cleared.

### High 3: Upgrade-paired rollback is not bound to a compatible restore executable

The design verifies the current baseline schema and then says a failed schema-affecting upgrade can
restore a pre-upgrade backup before rolling back image/config. The new image's verifier may no longer
accept the old schema contract, so that sequence is not deterministic. A Git commit string alone is
also not an immutable deployable artifact identity.

Required revision:

- Record the application/ops image digest, deploy-lock hash, Compose config hash, backend, backup
  format version, and schema-contract version in the backup/upgrade record.
- Freeze v0.1 restore compatibility as exact schema-contract matching.
- For paired rollback, stop writers, select the recorded old image/config without starting the API,
  run verify/restore from that exact ops image, and only then start the old API/proxy pair.
- Alternatively, introduce versioned read-only validators for every supported historical contract;
  that larger option is not required for D6 v0.1.
- Add a test where a newer/current validator rejects the old backup but the recorded old ops image or
  frozen validator accepts and restores it.

### Medium 4: Backup DB and manifest are not an atomic publication unit

Two sibling `os.replace()` calls cannot atomically publish a DB and manifest together. A crash between
them leaves an orphan that the current design does not classify or clean. The manifest is also called
immutable, while the later offsite hook is allowed to update `offsite_status`.

Required revision:

- Prefer a per-backup generation directory containing the DB and manifest, built under `tmp/` and
  atomically renamed into the date partition on the same filesystem.
- If sibling files are retained, publish the manifest last as the sole commit marker and define
  deterministic orphan detection/cleanup; never describe the pair as atomically published.
- Keep the manifest immutable. Any future offsite result must be a separate immutable receipt/status
  artifact linked by `backup_id` and manifest hash.

### Medium 5: Backup contention needs a total operation deadline

Python's `Connection.backup()` performs iterative attempts and exposes `pages`, `progress`, and
`sleep`. The design defines a busy timeout but no total backup deadline. A persistently busy source
must not leave a scheduler invocation waiting without a bounded end.

Required revision:

- Add `--max-duration-seconds` with a conservative production default.
- Enforce it through the progress callback/connection interruption boundary and map expiration to a
  stable fail-closed reason and exit code.
- Test persistent busy/lock behavior and prove no artifact or manifest is published after deadline.

The read-only production mount remains acceptable for D6. SQLite WAL read-only access depends on
readable existing WAL/SHM files, directory write access, or immutable mode. D6 must not use immutable
against a live database. Add a real Linux test for the read-only mount with active WAL/SHM and a
deterministic failure test when SQLite cannot establish a valid read-only snapshot.

References:

- Python 3.10 `sqlite3.Connection.backup`: https://docs.python.org/3.10/library/sqlite3.html#sqlite3.Connection.backup
- SQLite WAL read-only rules: https://www.sqlite.org/wal.html#read_only_databases

## Manager Decisions

1. Approve the default read-only, no-checkpoint production backup path. Do not add writable
   maintenance/checkpoint mode in D6 v0.1.
2. Retention and automatic pruning remain deferred to D7. D6 never auto-deletes verified backups or
   restore quarantine.
3. Production restore must hard-reject SQLAlchemy manifests. No escape hatch in D6 v0.1.
4. Off-ECS transfer remains a documented interface only. Do not add an executable hook, CLI option,
   network access, or mutable offsite fields in D6 v0.1.
5. `BEGIN IMMEDIATE` is only a preflight signal. It must be rolled back and closed before file
   movement and must not be described as proof that arbitrary host writers cannot start later. The
   authoritative v0.1 gate remains stopped Compose writers plus explicit operator confirmation.

## Required Test Additions

- Crash after each old-set per-file journal write, rename, and directory fsync.
- Reconcile split live/quarantine sets by path and digest; reject missing, duplicate, and mismatched
  files.
- Complete two independent restores sequentially with distinct restore IDs.
- Crash during terminal journal retirement and successfully start/resume the correct next operation.
- Exact old-image/schema-contract paired rollback.
- Atomic generation publication or manifest-last orphan handling.
- Total backup deadline under persistent contention.
- Linux read-only WAL backup with active API-like writes and readable WAL/SHM.

## Approval Gate

D6 implementation is authorized. Keep the approved scope narrow: backup/verify/restore library and
CLIs, read-only validators, D6 Compose profile activation, tests, and operator documentation only.
Do not modify business schemas, Session/Auth/Streaming/Memory policy, D5/D7/D8 behavior, frontend,
production data, or local secrets. T-0140 remains `pending` until implementation, full regression,
Linux read-only WAL validation, and a complete staging backup/restore drill receive Manager review.
