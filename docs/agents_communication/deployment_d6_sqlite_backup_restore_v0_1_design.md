# Deployment D6 SQLite Backup/Verify/Restore v0.1 Design

- Worker: Worker Session 21 / Deployment D6
- Status: Manager Approved; D6 implementation authorized
- Date: 2026-08-07
- Baseline: `9af2eb7`
- Scope: T-0140 D6 only: SQLite online backup, independent verification, restore, rollback,
  runbook, tests, and Compose profile activation.
- Non-goals: no business schema change, no Session/Auth/Streaming/Memory/Character/RAG policy
  change, no D5/D7/D8 work, no `frontend/` work, no Manager-owned task status update.

## 0. Executive Summary

D6 adds an operations-only persistence safety loop around the existing first-release deployment:

```text
api container, single Uvicorn writer
  -> /var/lib/aemeath/data/aemeath.db

ops profile, no network, no Provider secrets
  -> SQLite backup API online snapshot
  -> manifest
  -> independent read-only verify
  -> documented off-ECS encrypted copy interface only

restore profile, no network, no Provider secrets, writers stopped
  -> verify backup
  -> journaled install with per-file movement intents
  -> quarantine old main DB + -wal + -shm as one set
  -> verify installed candidate
  -> retire active restore pointer
  -> rollback if any phase fails
```

The backup script must never copy the live database file. It opens the source database read-only and
uses `sqlite3.Connection.backup(...)` to write a temporary candidate backup in the backup root, then
fsyncs the candidate, verifies it, writes an immutable manifest, and publishes the backup by
renaming one complete per-backup generation directory on the same filesystem. WAL and SHM sidecars
are not copied into the backup artifact because the backup API materializes a complete, consistent
database image.

Restore is intentionally stricter than backup. It refuses to run unless all writers are stopped and
the operator passes an explicit `--confirm-writers-stopped` flag. It treats
`aemeath.db`, `aemeath.db-wal`, and `aemeath.db-shm` as one database set. The old set is moved to a
quarantine directory before the candidate is installed; every file move has a durable journal intent
and completion record. Old sidecars are never reused with a new main database.

This revision incorporates the Manager review saved in
`docs/agents_communication/deployment_d6_sqlite_backup_restore_v0_1_manager_review.md`: D6 keeps the
read-only/no-checkpoint production backup path, never auto-deletes backups or quarantine, hard-rejects
SQLAlchemy manifests in production restore, documents off-ECS transfer without implementing it, and
treats `BEGIN IMMEDIATE` only as a preflight signal.

## 1. Reviewed Current Boundary

Current implementation facts that D6 must preserve:

- `compose.yaml` has only `api` and `proxy` as default resident services. `ops` and `restore`
  profiles exist but currently execute `profile_guard.py` and exit `78`.
- Production default is `sqlite_raw`; `sqlalchemy` is still an opt-in SQLite-backed development/test
  backend.
- The API container mounts `/var/lib/aemeath/data` read-write and owns the live DB path.
- `ops` currently mounts data read-only, logs read-only, backup root read-write, and has
  `network_mode: none`.
- `restore` currently mounts data read-write, backup root read-only, and has `network_mode: none`.
- `SQLiteStore` initializes `app/db/schema.sql`, then applies fail-fast migrations for sessions,
  accounts, Memory artifacts, Browser Auth, and streaming chat idempotency.
- `app/db/readiness.py` already uses a short-lived read-only SQLite URI and does not call Provider
  services.
- Browser Auth and streaming chat migrations include exact structural validation helpers and
  `foreign_key_check`; D6 should reuse that style for verification, but must not call store
  initialization against a backup because initialization may mutate schemas.

## 2. Design Principles

1. Online backup uses SQLite's backup API only. Direct file copy of a live DB is forbidden.
2. Verification is independent, repeatable, and read-only. It must not create tables, apply
   migrations, checkpoint, vacuum, attach a mutable sidecar, or instantiate a store facade.
3. Restore is a maintenance operation. No API, Memory Worker, ad hoc Python shell, SQLite browser,
   or other writer may hold the live database.
4. Restore is journaled and reentrant. A crash at any stage leads either to rollback to the old set
   or completion of the candidate install after another full verify.
5. Fail closed on uncertainty: lock contention, backup deadline expiry, malformed paths, wrong
   schema, hash mismatch, corruption, permission denial, disk exhaustion, sidecar ambiguity, split
   live/quarantine state, or incomplete journal state.
6. Logs and manifests contain only safe metadata: sizes, hashes, schema fingerprints, migration IDs,
   table names, counts, status buckets, timestamps, and reason codes. They never contain message
   content, prompts, credentials, raw tokens, cookies, raw IPs, provider bodies, or SQL values from
   user content.
7. Off-ECS backup is documentation only in D6. No hook script, CLI option, cloud SDK, OSS bucket,
   KMS provider, network access, or mutable offsite manifest field is introduced in this round.

## 3. Proposed Files

New files after approval:

| File | Purpose |
|---|---|
| `app/deployment/sqlite_backup.py` | Shared backup/verify/restore library with path, fsync, manifest, journal, and read-only schema checks. |
| `scripts/deployment/backup_sqlite.py` | CLI entrypoint for online backup. |
| `scripts/deployment/verify_sqlite_backup.py` | CLI entrypoint for independent verification. |
| `scripts/deployment/restore_sqlite.py` | CLI entrypoint for journaled restore and rollback. |
| `tests/deployment/test_sqlite_backup_restore.py` | Unit/integration coverage using temporary DBs and temporary directories only. |
| `docs/deployment.md` | Add D6 operator runbook after implementation approval. |
| `docs/persistence.md` | Add SQLite backup/restore contract after implementation approval. |

Modified files after approval:

| File | Change |
|---|---|
| `compose.yaml` | Replace `ops` and `restore` fail-closed placeholder commands with approved D6 CLIs while preserving profile isolation. |
| `scripts/deployment/profile_guard.py` | Keep `memory-worker` disabled; remove only the D6 guard entries when D6 is implemented. |
| `tests/deployment/test_compose_contract.py` | Assert D6 profile mounts, commands, no network, no Provider secrets, and default services unchanged. |

No changes are planned to `app/db/schema.sql`, store ports, domain services, API routes, frontend, or
Manager task status.

No offsite executable is planned for D6 v0.1. The runbook may describe a future operator-owned,
out-of-band encrypted copy step, but D6 code must not add a hook script, CLI option, network access,
or mutable offsite status artifact.

## 4. CLI Contracts

All CLIs print one JSON object per final outcome. Safe progress events may be JSONL with
`event`, `stage`, `status`, `reason`, `backup_id`, and durations only. No command prints absolute
host secrets or content.

### 4.1 `backup_sqlite.py`

```bash
python scripts/deployment/backup_sqlite.py \
  --db-path /var/lib/aemeath/data/aemeath.db \
  --backup-root /var/backups/aemeath \
  --label nightly \
  --backend sqlite_raw
```

Arguments:

| Argument | Required | Contract |
|---|---:|---|
| `--db-path` | yes | Live SQLite main DB path. Must resolve to a regular non-symlink file inside an approved data root. |
| `--backup-root` | yes | Existing backup root. Must be non-symlink, writable by runtime, and not inside the data root. |
| `--label` | no | Safe token `[A-Za-z0-9_.-]{1,40}` such as `nightly` or `pre-upgrade`. |
| `--backend` | yes | `sqlite_raw` for production; `sqlalchemy` allowed only for tests/dev backup compatibility. |
| `--busy-timeout-ms` | no | Default `5000`. Applies to source read-only connection and backup loop. |
| `--pages-per-step` | no | Default `256`. Backup API page batch size. |
| `--sleep-ms` | no | Default `50`. Sleep between backup retries. |
| `--max-duration-seconds` | no | Default `300`. Total wall-clock deadline for backup API progress, destination verify, and publication. |
| `--json` | no | Emit final JSON object. |

Output artifact layout:

```text
/var/backups/aemeath/
  backups/
    2026/08/07/
      aemeath-20260807T153012Z-nightly-<backup_id>/
        aemeath.db
        manifest.json
  tmp/
    generation-<run_id>/
      aemeath.db.tmp
      manifest.json.tmp
```

Backup algorithm:

1. Resolve and validate all paths.
2. Refuse if backup root is under the live data root or if the final generation directory already
   exists.
3. Open source with `file:<db>?mode=ro` and destination as a new exclusive temp file.
4. Enable `PRAGMA foreign_keys=ON` and `PRAGMA query_only=ON` on the source connection.
5. Do not checkpoint the production source by default. The D4 `ops` source data mount is read-only,
   and the backup API does not require copying or checkpointing WAL sidecars to produce a
   consistent snapshot.
6. Use `source.backup(destination, pages=..., sleep=..., progress=...)`. The progress callback checks
   the total deadline after every batch and aborts through a controlled exception/connection
   interruption boundary. `SQLITE_BUSY`, `SQLITE_LOCKED`, or deadline expiry is treated as
   fail-closed and no generation directory is published.
7. Run `PRAGMA quick_check` or `integrity_check` on the destination before publication. D6 should
   use full `integrity_check` for scheduled backup; tests may use a smaller fixture.
8. Fsync destination DB file, close it, fsync the generation temp directory, compute SHA-256, build
   immutable manifest, fsync manifest.
9. Rename `aemeath.db.tmp` to `aemeath.db` and `manifest.json.tmp` to `manifest.json` inside the temp
   generation directory; fsync the temp generation directory again.
10. Atomically publish the whole generation directory by renaming
    `tmp/generation-<run_id>` to `backups/YYYY/MM/DD/aemeath-...-<backup_id>`.
11. Fsync the date partition and backup root. A complete backup is defined by the presence of a
    generation directory containing both `aemeath.db` and `manifest.json`.
12. Off-ECS transfer is not invoked by D6 code. Operators may copy verified generation directories
    out of band under a separately approved encrypted process.

Exit codes:

| Code | Meaning |
|---:|---|
| `0` | Backup and local verify succeeded. |
| `2` | CLI usage or invalid argument. |
| `10` | Path validation failed. |
| `11` | Destination already exists or would overwrite. |
| `12` | Permission denied or ownership/mode contract failed. |
| `13` | Disk space or fsync failure. |
| `20` | SQLite source unavailable, locked, or backup API failed. |
| `21` | Backup candidate integrity/schema verification failed. |
| `22` | Manifest write, hash, or publish failed. |
| `23` | Total backup deadline exceeded; no artifact published. |
| `70` | Unexpected internal error, sanitized. |

### 4.2 `verify_sqlite_backup.py`

```bash
python scripts/deployment/verify_sqlite_backup.py \
  --backup-dir /var/backups/aemeath/backups/2026/08/07/aemeath-...-bkp_01K \
  --backend sqlite_raw
```

Arguments:

| Argument | Required | Contract |
|---|---:|---|
| `--backup-dir` | yes | Published generation directory containing `aemeath.db` and immutable `manifest.json`. |
| `--backend` | yes | Must match manifest backend. |
| `--expected-source-db` | no | Optional basename or configured live path hash for wrong-database checks. |
| `--json` | no | Emit final JSON object. |

Verify checks:

1. Path containment and no symlink/path traversal.
2. Manifest JSON parse, exact format version, required fields, no unknown critical fields, and no
   mutable offsite status fields.
3. File size and SHA-256 exactly match manifest. `manifest.json` is the immutable commit record
   inside a generation directory; verify rejects orphan DBs outside a generation directory.
4. Open DB read-only using URI `mode=ro`; set `PRAGMA query_only=ON`; enable and assert
   `foreign_keys=ON`.
5. `PRAGMA integrity_check` returns exactly `ok`.
6. `PRAGMA foreign_key_check` returns no rows.
7. Schema contract:
   - required table set for current baseline;
   - `schema_migrations` exists and contains expected migration markers;
   - Browser Auth structural contract and streaming chat structural contract pass read-only
     validators;
   - active Memory identity index contract passes;
   - manifest `schema_contract_version` must exactly match the validator contract used by the
     running ops image;
   - SQLAlchemy manifests are accepted only for local/dev verify, never for production restore.
8. SQLite identity:
   - `sqlite_version`;
   - `PRAGMA application_id`, `user_version`, `page_size`, `page_count`;
   - normalized `sqlite_schema` fingerprint.
9. Safe counts match manifest exactly unless `--refresh-manifest` is added in a future separately
   approved command. D6 verify never mutates the manifest.
10. Deployment identity fields are present and syntactically valid: app/ops image digest, deploy-lock
    hash, Compose config hash, backend, backup format version, and schema-contract version.

Exit codes:

| Code | Meaning |
|---:|---|
| `0` | Backup is verified and restorable. |
| `2` | CLI usage or invalid argument. |
| `10` | Path validation failed. |
| `12` | Permission denied. |
| `20` | SQLite open/read failed. |
| `21` | Hash, size, integrity, FK, schema, wrong DB, or safe count mismatch. |
| `70` | Unexpected internal error, sanitized. |

### 4.3 `restore_sqlite.py`

```bash
python scripts/deployment/restore_sqlite.py \
  --data-root /var/lib/aemeath/data \
  --db-name aemeath.db \
  --backup-dir /var/backups/aemeath/backups/2026/08/07/aemeath-...-bkp_01K \
  --journal-root /var/lib/aemeath/data/.restore-journal \
  --confirm-writers-stopped \
  --expected-backend sqlite_raw
```

Arguments:

| Argument | Required | Contract |
|---|---:|---|
| `--data-root` | yes | Approved production data root. Must be non-symlink, writable, and not world-writable. |
| `--db-name` | yes | Basename only, default `aemeath.db`; no separators. |
| `--backup-dir` | yes | Verified generation directory, mounted read-only. |
| `--journal-root` | yes | Existing or creatable directory under data root. |
| `--confirm-writers-stopped` | yes | Required explicit operator acknowledgement. |
| `--expected-backend` | yes | Must match manifest backend. |
| `--restore-id` | no | Optional new restore ID. If omitted, generated by the script. |
| `--resume-restore-id` | no | Resume one exact nonterminal restore journal. |
| `--rollback-restore-id` | no | Resume one exact restore journal and roll back to quarantined old set. |
| `--force-abort-empty-candidate` | no | Only allowed when no live DB and journal says candidate was never installed. |
| `--json` | no | Emit final JSON object. |

Restore preconditions:

- The command must run through the `restore` Compose profile with `network_mode: none`.
- `api` and any future writer profile must be stopped, not merely unhealthy.
- The operator runbook must require:

```bash
sudo docker compose --env-file /etc/aemeath/prod.env stop api
sudo docker compose --env-file /etc/aemeath/prod.env ps
sudo docker compose --env-file /etc/aemeath/prod.env --profile restore run --rm restore ...
```

- The script additionally tries a short `BEGIN IMMEDIATE` against the current DB and immediately
  rolls it back and closes the connection before file movement. This is only a preflight signal and
  not proof that arbitrary host writers cannot start later. The authoritative v0.1 gate is stopped
  Compose writers plus explicit operator confirmation.

Restore algorithm:

1. Verify backup generation directory and immutable manifest read-only.
2. Resolve `data_root / db_name`, `-wal`, and `-shm`; reject symlinks and non-regular sidecars.
3. Create or load one restore journal directory and an active pointer with an exclusive lock file.
4. Stage a prepared candidate copy from `backup-dir/aemeath.db` into journal `prepared/` using
   exclusive create, fsync file and directory, and verify the staged candidate read-only.
5. Freeze the old-set inventory for `aemeath.db`, `aemeath.db-wal`, and `aemeath.db-shm`: expected
   presence, size, SHA-256, live path, quarantine path, and movement state.
6. For each present file, journal and fsync an intent before rename, rename exactly one file, fsync
   source and destination directories, then journal and fsync the completed movement record.
7. Reconcile the inventory before declaring `old_quarantined`: each expected file must have exactly
   one valid digest-matching copy in quarantine and no old sidecar may remain live. Missing,
   duplicate, or mismatched copies fail closed.
8. Install candidate as `data_root/aemeath.db` with no `-wal` or `-shm` sidecars.
9. Fsync `data_root`.
10. Open installed DB read-only and run full verify again.
11. Mark the restore journal `verified`, archive/retire the active pointer, and fsync the journal
    root so another restore can start.
12. Leave quarantine in place until D8 or operator retention policy explicitly prunes it. D6 does
    not auto-delete the old set.

Exit codes:

| Code | Meaning |
|---:|---|
| `0` | Restore completed and installed candidate verified. |
| `2` | CLI usage or invalid argument. |
| `10` | Path validation failed. |
| `12` | Permission denied or writer-stopped confirmation missing. |
| `13` | Disk space or fsync failure. |
| `20` | Current DB writer lock, SQLite open, or verification failed. |
| `21` | Backup verification failed. |
| `31` | Journal conflict, invalid stage, or concurrent restore detected. |
| `32` | Candidate install failed and rollback succeeded. |
| `33` | Candidate install failed and rollback also failed; manual Manager intervention required. |
| `70` | Unexpected internal error, sanitized. |

## 5. Manifest Schema

Format version is `aemeath.sqlite_backup.manifest.v1`. Manifest files are immutable once
published. Any future schema addition must be backward compatible or use a new format version.

```json
{
  "format_version": "aemeath.sqlite_backup.manifest.v1",
  "backup_id": "bkp_01K...",
  "created_at_utc": "2026-08-07T15:30:12Z",
  "label": "nightly",
  "source": {
    "db_basename": "aemeath.db",
    "db_path_sha256": "sha256-of-canonical-live-path-string",
    "backend": "sqlite_raw",
    "compose_project": "aemeath"
  },
  "deployment": {
    "git_commit": "9af2eb7",
    "app_image_digest": "sha256:<app-image-digest>",
    "ops_image_digest": "sha256:<same-app-image-digest>",
    "deploy_lock_sha256": "hex",
    "compose_config_sha256": "hex"
  },
  "artifact": {
    "generation_dir": "aemeath-20260807T153012Z-nightly-bkp_01K",
    "db_filename": "aemeath.db",
    "manifest_filename": "manifest.json",
    "size_bytes": 123456,
    "sha256": "hex",
    "page_size": 4096,
    "page_count": 31
  },
  "sqlite": {
    "sqlite_version": "3.x.y",
    "application_id": 0,
    "user_version": 0,
    "journal_mode_seen_on_source": "wal",
    "checkpoint": {"mode": "none", "result": "not_attempted"},
    "integrity_check": "ok",
    "foreign_key_check_rows": 0
  },
  "schema": {
    "schema_contract_version": "aemeath.sqlite.schema_contract.v1",
    "sqlite_schema_sha256": "hex",
    "migration_ids": [
      "browser_auth_v0_1",
      "memory_embedding_artifact_v1",
      "memory_exact_active_identity_v1",
      "memory_scope_provenance_v1",
      "streaming_chat_idempotency_v0_2"
    ],
    "browser_auth_schema_revision": "browser_auth_v0_1",
    "browser_auth_contract_fingerprint": "hex",
    "streaming_chat_contract": "streaming_chat_idempotency_v0_2",
    "active_memory_identity_index": "valid"
  },
  "safe_counts": {
    "accounts_total": 1,
    "sessions_by_status": {"active": 1, "ended": 0, "archived": 0},
    "messages_total": 2,
    "chat_requests_by_status": {"in_progress": 0, "completed": 0, "failed": 0},
    "memories_by_status": {"active": 0, "archived": 0, "disabled": 0},
    "memory_jobs_by_status": {"pending": 0, "running": 0, "completed": 0, "failed": 0},
    "browser_auth_sessions_total": 0,
    "relationship_states_total": 1
  },
  "offsite_interface": "documented_only"
}
```

Safe count rules:

- Count buckets use fixed status values and zero-fill missing buckets.
- Counts never include IDs, usernames, message text, memory content, prompt data, token digests, or
  credential material.
- Verify recomputes counts from a read-only transaction and compares the exact JSON object.
- `in_progress` streaming records are allowed in a backup produced during concurrent API writes,
  because online backup is a snapshot. Restore runbook must note that orphan cleanup remains the
  streaming service's normal runtime responsibility after API restart.
- The manifest is immutable. Any future off-ECS copy result must be a separate immutable receipt
  linked by `backup_id` and the manifest SHA-256; D6 v0.1 does not create such receipts.
- `deployment.ops_image_digest`, `deploy_lock_sha256`, `compose_config_sha256`, and
  `schema_contract_version` are the compatibility identity for upgrade-paired rollback. A plain Git
  commit is metadata, not sufficient restore identity.

## 6. Restore Journal Schema

Journal root:

```text
/var/lib/aemeath/data/.restore-journal/
  active.pointer.json
  active.lock
  journals/
    <restore_id>/
      journal.json
      prepared/candidate.db
      quarantine/old/aemeath.db
      quarantine/old/aemeath.db-wal
      quarantine/old/aemeath.db-shm
      quarantine/failed_candidate/
  history/
    <restore_id>.terminal.json
```

`active.pointer.json` is the only active operation pointer. It contains only:

```json
{
  "format_version": "aemeath.sqlite_restore.active_pointer.v1",
  "restore_id": "rst_01K...",
  "journal_path": "journals/rst_01K/journal.json",
  "stage": "prepared",
  "updated_at_utc": "2026-08-07T16:02:00Z"
}
```

`journal.json` format version is `aemeath.sqlite_restore.journal.v1`.

```json
{
  "format_version": "aemeath.sqlite_restore.journal.v1",
  "restore_id": "rst_01K...",
  "started_at_utc": "2026-08-07T16:00:00Z",
  "updated_at_utc": "2026-08-07T16:02:00Z",
  "stage": "prepared",
  "backup": {
    "backup_id": "bkp_01K...",
    "generation_dir": "aemeath-...",
    "manifest_sha256": "hex",
    "artifact_sha256": "hex",
    "backend": "sqlite_raw",
    "ops_image_digest": "sha256:<recorded-old-ops-image>",
    "schema_contract_version": "aemeath.sqlite.schema_contract.v1"
  },
  "paths": {
    "data_root": "/var/lib/aemeath/data",
    "db_name": "aemeath.db",
    "journal_dir": ".restore-journal/journals/rst_01K",
    "prepared_dir": ".restore-journal/journals/rst_01K/prepared",
    "quarantine_dir": ".restore-journal/journals/rst_01K/quarantine/old"
  },
  "old_set": [
    {
      "name": "aemeath.db",
      "expected_present": true,
      "size_bytes": 123456,
      "sha256": "hex",
      "live_path": "/var/lib/aemeath/data/aemeath.db",
      "quarantine_path": ".restore-journal/journals/rst_01K/quarantine/old/aemeath.db",
      "state": "live",
      "last_intent": null,
      "completed_at_utc": null
    },
    {
      "name": "aemeath.db-wal",
      "expected_present": true,
      "size_bytes": 32768,
      "sha256": "hex",
      "live_path": "/var/lib/aemeath/data/aemeath.db-wal",
      "quarantine_path": ".restore-journal/journals/rst_01K/quarantine/old/aemeath.db-wal",
      "state": "move_completed",
      "last_intent": "move_live_to_quarantine",
      "completed_at_utc": "2026-08-07T16:01:00Z"
    },
    {
      "name": "aemeath.db-shm",
      "expected_present": false,
      "size_bytes": null,
      "sha256": null,
      "live_path": "/var/lib/aemeath/data/aemeath.db-shm",
      "quarantine_path": ".restore-journal/journals/rst_01K/quarantine/old/aemeath.db-shm",
      "state": "absent",
      "last_intent": null,
      "completed_at_utc": null
    }
  ],
  "candidate": {
    "prepared_sha256": "hex",
    "installed_sha256": null
  },
  "history": [
    {"stage": "started", "at_utc": "2026-08-07T16:00:00Z"},
    {"stage": "prepared", "at_utc": "2026-08-07T16:00:30Z"}
  ],
  "failure": null
}
```

Allowed stages:

| Stage | Meaning |
|---|---|
| `started` | Journal created; no file movement yet. |
| `prepared` | Candidate copied into journal and verified; live set unchanged. |
| `old_quarantine_in_progress` | At least one old-set file move intent or completion exists; reentry must reconcile per-file state. |
| `old_quarantined` | All expected old-set files have exactly one valid copy in quarantine and none remain live. |
| `candidate_installed` | Candidate installed as main DB; no candidate sidecars installed. |
| `verified` | Installed candidate passed full read-only verify. |
| `rolled_back` | Old set restored from quarantine. |
| `retired` | Terminal journal archived to history and active pointer cleared. |
| `failed` | Fail-closed terminal state needing Manager/operator intervention. |

Journal writes:

- Use exclusive create for a temp JSON file, fsync it, then `os.replace` to `journal.json`.
- Before every old-set rename, write and fsync `last_intent = move_live_to_quarantine` for that file.
- After every old-set rename, fsync both source and destination directories, then write and fsync
  `state = move_completed` for that file.
- A single `old_quarantined` stage is legal only after all expected files have been reconciled by
  digest and location.
- The active pointer is updated with the same temp-file, fsync, and `os.replace` strategy whenever
  the active stage changes.
- Terminal finalization writes `history/<restore_id>.terminal.json`, fsyncs it, atomically removes or
  clears `active.pointer.json`, fsyncs the journal root, and leaves the per-restore journal and
  quarantine retained.
- Hold `active.lock` for the whole restore process where POSIX locking is available. On platforms
  without POSIX locks, exclusive lock-file creation still prevents ordinary duplicate runs.

## 7. Crash Reentry

The restore command always loads the active pointer before starting a new restore.

| Reentry stage | Required action |
|---|---|
| no active pointer | Start new restore with a new `restore_id`. Retained history and quarantine do not block a new restore. |
| active pointer references missing journal | Fail closed with `restore_journal_missing`. |
| nonterminal active pointer, no matching `--resume-restore-id` or `--rollback-restore-id` | Fail closed; never start a second restore over an active nonterminal one. |
| `started` | If no prepared candidate exists and no file movement exists, mark failed or abort only with explicit operator action. |
| `prepared` | Re-verify candidate. Continue to old-set inventory or abort with live DB unchanged. |
| `old_quarantine_in_progress` | Reconcile every old-set file by digest and location. For each expected file, exactly one valid copy may exist in live or quarantine. Missing, duplicate, mismatched, or unexpected sidecar copies fail closed. Continue moving only files still proven live. |
| `old_quarantined` | Confirm all expected files are in quarantine and no old sidecar remains live; then install candidate. |
| `candidate_installed` | Re-run full verify on installed DB. If it passes, mark `verified`; if it fails, rollback from quarantine. |
| `verified` | Retire active pointer into history. If pointer retirement was interrupted, repeat retirement idempotently. |
| `rolled_back` | Retire active pointer into history. If pointer retirement was interrupted, repeat retirement idempotently. |
| `retired` journal with no active pointer | Retained history only; does not affect new restore. |
| `failed` | Refuse automatic action unless `--rollback-restore-id` is provided and the old set is complete. |

Rollback from failed restore:

1. Stop writers again and acquire the restore lock.
2. If candidate exists at live path, move it to `quarantine/<restore_id>/failed_candidate/` with any
   newly created candidate sidecars.
3. For each old-set file, journal intent, rename from quarantine to live, fsync both directories,
   and journal completion. The same per-file duplicate/missing/digest checks apply.
4. Fsync data root.
5. Verify restored old main DB read-only. If old sidecars existed, SQLite may recover them on open;
   verify must still pass.
6. Mark `rolled_back`, then retire the active pointer to history.

If rollback cannot prove the old set is complete or cannot verify it, exit `33` and require Manager
intervention. The script must never silently combine old sidecars with the candidate.

Terminal pointer retirement:

1. Write `history/<restore_id>.terminal.json` with final stage, backup identity, candidate/old-set
   digests, and safe timestamps.
2. Fsync the history file and history directory.
3. Atomically replace `active.pointer.json` with an empty tombstone or remove it through an
   implementation-specific temp-file strategy that is idempotent after crash.
4. Fsync the journal root.
5. A later invocation with no active pointer may start a new restore. A later invocation that sees a
   terminal pointer repeats retirement and then exits successfully.

## 8. Path, Symlink, Overwrite, And Fsync Strategy

Path rules:

- All configured roots are absolute.
- `Path.resolve(strict=True)` is used for existing roots and source files.
- Any symlink in the candidate path, source DB path, backup root, data root, journal root, or parent
  components is rejected.
- `--db-name` and generated artifact basenames must be basenames only.
- Backup generation directory names are generated by the script, not supplied by the operator.
- Restore refuses if the backup generation directory resolves inside the live data root.
- Restore refuses if `data_root` resolves inside `backup_root` or vice versa.
- Temp files and temp generation directories use exclusive create. Published generation directories
  must not exist.

Overwrite policy:

- Backup never overwrites a generation directory, `.db`, or manifest.
- A complete backup is a generation directory containing both `aemeath.db` and `manifest.json`.
  Orphan temp generation directories under `tmp/` may be reported and ignored by verify; D6 does not
  auto-delete them. A published generation directory missing either file is invalid and verify
  rejects it.
- Restore may replace only `data_root/db_name`, `db_name-wal`, and `db_name-shm`, and only after
  they are recorded and moved to quarantine.
- Any unexpected sibling such as `aemeath.db-journal` or `aemeath.db.bak` is not touched. The
  command reports it as `unexpected_sidecar` and fails closed until Manager approves handling.

Fsync policy:

- Fsync every newly written DB, manifest, journal, active pointer, history record, and copied
  candidate file.
- Fsync directories after create, rename, replace, or unlink, including both source and destination
  directories for every old-set file movement.
- On Windows tests, directory fsync is skipped because the local platform does not expose the same
  POSIX semantics; tests assert the helper is called or gracefully skipped.
- Disk-full and fsync errors are hard failures with no best-effort publication.

## 9. SQLite And Schema Verification Details

Read-only verify must not call:

- `SQLiteStore(db_path)`;
- `SqlAlchemyStore(db_path)`;
- `Base.metadata.create_all(...)`;
- `ensure_browser_auth_schema(...)` or `ensure_streaming_chat_schema(...)` if those functions would
  mutate missing schemas.

Instead, D6 should add read-only validators that share the exact structural predicates:

- Browser Auth: expose a `validate_browser_auth_schema_readonly(connection)` helper that performs
  `_validate_schema` and `_require_foreign_keys_clean` only.
- Streaming chat: expose a `validate_streaming_chat_schema_readonly(connection)` helper that
  performs `_validate_schema(... expected v0.2 checks ...)` and FK checks only.
- Active Memory index: reuse `has_valid_active_memory_identity_index(...)`.
- Raw baseline: verify required tables and migration IDs without executing `schema.sql`.
- SQLAlchemy baseline: verify the current experimental SQLite schema only for local/dev verify when
  manifest/backend is `sqlalchemy`; production restore hard-rejects SQLAlchemy manifests with no D6
  escape hatch.

Manifest `schema.sqlite_schema_sha256` is computed from a normalized `sqlite_schema` projection:

```text
type | name | tbl_name | normalized sql
```

Internal SQLite autoindexes are excluded unless they are the only representation of a required
constraint. Required named indexes and triggers are included.

## 10. WAL Sidecar Policy

Backup:

- Do not copy `aemeath.db-wal` or `aemeath.db-shm`.
- Backup API reads a consistent snapshot from SQLite, including WAL-visible committed pages.
- No checkpoint is attempted in the default production `ops` profile because the data mount is
  read-only.
- No writable maintenance/checkpoint mode is added in D6 v0.1.
- D6 must not use SQLite immutable mode against a live database.
- Linux validation must cover a read-only source mount while the database is in WAL mode with
  readable existing WAL/SHM files, plus deterministic failure when SQLite cannot establish a valid
  read-only WAL snapshot.
- The manifest records whether the source appeared to be in WAL mode, but not WAL bytes.

Restore:

- Treat old main DB and sidecars as a single quarantine set.
- Candidate install starts with only the main DB file.
- Delete or move any live candidate sidecars created during failed verify before rollback.
- Never reuse old `-wal` or `-shm` with candidate main DB.

## 11. Raw SQLite And SQLAlchemy-on-SQLite Boundary

Production D6 target is `sqlite_raw`.

`sqlalchemy` support is limited to:

- backing up and verifying temporary dev/test SQLite files;
- contract parity tests where the backend already uses the same SQLite file model;
- proving that the D6 file and manifest mechanics do not depend on `SQLiteStore`.

Restore in production must reject a manifest with `source.backend != sqlite_raw`. There is no
override flag or escape hatch in D6 v0.1. D6 must not introduce Alembic or PostgreSQL behavior.

## 12. Failure Modes

| Failure | Backup behavior | Verify behavior | Restore behavior |
|---|---|---|---|
| API writes during backup | Backup API snapshot from a read-only source; success if backup API, deadline, and verify pass. | Not applicable. | Writers must be stopped. |
| Backup deadline exceeded | Exit `23`; remove or leave only unpublished temp generation directory; no manifest commit. | Not applicable. | Not applicable. |
| SQLite backup busy/locked | Exit `20`; no published generation directory. | Not applicable. | Refuse if current DB cannot acquire writer-lock preflight. |
| Corrupt/truncated backup | Not published if candidate verify fails. | Exit `21`. | Refuse before touching live DB. |
| Wrong database | Manifest path hash/backend/schema/count mismatch fails. | Exit `21`. | Refuse before touching live DB. |
| Permission denied | Exit `12`, no partial publish. | Exit `12`. | Exit `12`; rollback if movement already began. |
| Disk full/fsync error | Exit `13`, temp remains quarantined or is cleaned safely. | Exit `20` or `21`. | Exit `13`; rollback from old quarantine. |
| Crash between old-set file renames | Not applicable. | Not applicable. | Reconcile every expected file by path and digest; continue only proven live files or fail closed. |
| Split live/quarantine set | Not applicable. | Not applicable. | Missing, duplicate, mismatched, or unexpected copies fail closed; never combine guessed sidecars. |
| Terminal journal blocks next restore | Not applicable. | Not applicable. | Terminal pointer retirement is idempotent; retained history/quarantine do not block a new restore. |
| Candidate verify fails after install | Not applicable. | Not applicable. | Move failed candidate aside, restore old set, verify old set. |
| New image rejects old backup in paired rollback | Not applicable. | Current-image verify may fail by schema-contract mismatch. | Operator runs verify/restore from recorded old ops image/config that created the backup. |
| Published generation dir missing DB or manifest | Not applicable. | Exit `21`; classify as invalid generation, not a usable backup. | Refuse before touching live DB. |
| Rollback verify fails | Not applicable. | Not applicable. | Exit `33`, Manager intervention. |

All failure logs use stable reason codes such as `path_escape`, `backup_deadline_exceeded`,
`backup_hash_mismatch`, `sqlite_integrity_failed`, `foreign_key_violation`,
`schema_contract_invalid`, `restore_journal_conflict`, `restore_file_missing`,
`restore_file_duplicate`, `restore_file_digest_mismatch`, `candidate_verify_failed`,
`rollback_verify_failed`.

## 13. Compose Profile Design

After Manager approval, `compose.yaml` should change only the D6 one-shot profiles:

```yaml
ops:
  command:
    - python
    - /opt/aemeath/scripts/deployment/backup_sqlite.py
    - --db-path
    - /var/lib/aemeath/data/aemeath.db
    - --backup-root
    - /var/backups/aemeath
    - --backend
    - sqlite_raw
  network_mode: none
  volumes:
    - /var/lib/aemeath/data:/var/lib/aemeath/data:ro
    - /var/backups/aemeath:/var/backups/aemeath:rw
```

`restore` keeps data read-write and backup root read-only:

```yaml
restore:
  command:
    - python
    - /opt/aemeath/scripts/deployment/restore_sqlite.py
  network_mode: none
  volumes:
    - /var/lib/aemeath/data:/var/lib/aemeath/data:rw
    - /var/backups/aemeath:/var/backups/aemeath:ro
```

Profile invariants:

- `network_mode: none`.
- No `ports`, no `expose`.
- No DeepSeek, SiliconFlow, bearer, web-auth, or Provider secrets.
- Same non-root UID/GID `10001:10001`.
- Read-only root filesystem and bounded tmpfs preserved.
- `ops` may write only backup root and temp.
- `restore` may write only data root and journal.
- Default `docker compose up -d` still creates exactly `api` and `proxy`.

## 14. Operator Runbook

Nightly backup:

```bash
sudo docker compose --env-file /etc/aemeath/prod.env \
  --profile ops run --rm ops \
  python /opt/aemeath/scripts/deployment/backup_sqlite.py \
  --db-path /var/lib/aemeath/data/aemeath.db \
  --backup-root /var/backups/aemeath \
  --backend sqlite_raw \
  --label nightly
```

Pre-upgrade backup uses `--label pre-upgrade-<target-image-id-safe-token>`.

Independent verify:

```bash
sudo docker compose --env-file /etc/aemeath/prod.env \
  --profile ops run --rm ops \
  python /opt/aemeath/scripts/deployment/verify_sqlite_backup.py \
  --backup-dir /var/backups/aemeath/backups/.../aemeath-...-bkp_01K \
  --backend sqlite_raw
```

Restore:

```bash
sudo docker compose --env-file /etc/aemeath/prod.env stop api
sudo docker compose --env-file /etc/aemeath/prod.env ps
sudo docker compose --env-file /etc/aemeath/prod.env \
  --profile restore run --rm restore \
  python /opt/aemeath/scripts/deployment/restore_sqlite.py \
  --data-root /var/lib/aemeath/data \
  --db-name aemeath.db \
  --backup-dir /var/backups/aemeath/backups/.../aemeath-...-bkp_01K \
  --journal-root /var/lib/aemeath/data/.restore-journal \
  --expected-backend sqlite_raw \
  --confirm-writers-stopped
sudo docker compose --env-file /etc/aemeath/prod.env up -d api
sudo docker compose --env-file /etc/aemeath/prod.env up -d proxy
```

Upgrade paired rollback:

1. Before upgrade, create and verify `pre-upgrade` backup.
2. Record app/ops image digest, Caddy digest, deploy lock hash, Compose config hash, backend,
   backup format version, schema-contract version, and backup ID.
3. Upgrade image/config.
4. If upgrade fails before DB writes, roll back image/config only.
5. If upgrade writes DB and then fails, stop writers and select the recorded old image/config without
   starting the API.
6. Run verify/restore from the recorded old ops image because the current/new validator may reject
   the old schema contract.
7. Start the old API/proxy pair and run provider-free health/readiness/auth smoke.

The runbook must make clear that DB rollback without matching image/config rollback may be unsafe
after schema-affecting upgrades. D6 v0.1 freezes restore compatibility as exact
`schema_contract_version` matching; it does not implement historical validators for multiple schema
contracts.

## 15. RPO/RTO Acceptance

Current target:

- RPO: 24 hours.
- RTO: 2 hours.
- Schedule: nightly and before every upgrade.

Acceptance evidence:

- At least one scheduled backup artifact and manifest are produced in staging.
- Independent verify passes on the artifact.
- A restore drill from a verified backup completes within 2 hours, including writer stop, restore,
  restart, and provider-free smoke.
- The newest verified backup age is less than 24 hours at the time of drill.
- The runbook records operator start/end UTC, backup ID, restore ID, final image/config pair, and
  safe smoke results.

## 16. Security Logs

Allowed fields:

- `event`, `stage`, `status`, `reason`, `exit_code`;
- `backup_id`, `restore_id`, `label`;
- UTC timestamps and durations;
- file sizes and SHA-256 hashes of DB/manifest/journal artifacts;
- schema fingerprint, migration IDs, backend;
- safe counts and status buckets;
- documented off-ECS interface status such as `documented_only`, without destination, credential, or
  transfer receipt data.

Forbidden fields:

- message contents, memory contents, prompts, model responses, Provider payloads;
- API keys, bearer tokens, cookies, CSRF values, invitation/reset tokens;
- raw exception text that may include paths with secrets or SQL values;
- raw user IP or raw HTTP headers.

## 17. Tests

All destructive tests use temporary databases and temporary directories only.

Automated tests:

| Test | Coverage |
|---|---|
| `test_online_backup_during_api_like_writes` | Start a writer thread/process inserting rows; backup via API; verify consistent snapshot. |
| `test_backup_rejects_direct_copy_escape_paths` | Path traversal, symlink root, backup root under data root, and destination overwrite. |
| `test_manifest_hash_size_and_counts` | Manifest exact hash, file size, schema ID, and safe counts. |
| `test_verify_rejects_truncated_backup` | Truncate DB bytes; verify exits `21`. |
| `test_verify_rejects_corrupt_backup` | Flip bytes in a temp DB copy; integrity/hash failure. |
| `test_verify_rejects_wrong_database` | Valid SQLite file with wrong schema/backend/counts. |
| `test_restore_quarantines_sidecars_as_set` | Old `aemeath.db`, `-wal`, `-shm` moved together; candidate has no old sidecars. |
| `test_restore_crash_after_each_old_set_step` | Crash injection after every journal write, rename, and directory fsync for main, WAL, and SHM. |
| `test_restore_reconcile_split_sets` | Reconcile split live/quarantine sets by path and digest; reject missing, duplicate, and mismatched files. |
| `test_restore_crash_reentry_prepared` | Journal at `prepared` resumes safely. |
| `test_restore_crash_reentry_old_quarantine_in_progress` | Per-file old-set movement resumes only after digest/location reconciliation. |
| `test_restore_candidate_verify_failure_rolls_back` | Bad candidate after install restores old set. |
| `test_restore_refuses_running_writer` | Held writer lock causes fail-closed. |
| `test_two_independent_restores` | Complete two sequential restores with distinct restore IDs after terminal pointer retirement. |
| `test_restore_pointer_retirement_crash` | Crash during terminal pointer retirement recovers idempotently and permits the next operation. |
| `test_exact_old_image_schema_contract_rollback` | New/current validator rejects old backup; recorded old ops image or frozen validator accepts and restores it. |
| `test_backup_generation_directory_publication` | DB and manifest publish as one generation directory; incomplete generation directories are rejected. |
| `test_backup_total_deadline` | Persistent contention exceeds `--max-duration-seconds`; no generation directory is published. |
| `test_linux_read_only_wal_backup` | Linux read-only source mount with active WAL/SHM succeeds when SQLite read-only WAL requirements are met and fails deterministically otherwise. |
| `test_permissions_and_fsync_fail_closed` | Simulated permission/fsync/disk-full errors do not publish partial artifact. |
| `test_compose_d6_profiles` | `ops` and `restore` profile commands, mounts, no network, no secrets, default services unchanged. |

Staging drill:

1. Create isolated staging roots, never local production `data/aemeath.db`.
2. Start API/proxy with mock-safe credentials and no real Provider request.
3. Generate API writes during an `ops` online backup.
4. Verify backup independently.
5. Stop API, restore into staging data root, restart API/proxy.
6. Verify `/health`, `/ready`, auth rejection/acceptance shape, and that no Memory Worker exists.
7. Record safe IDs and metadata only.

## 18. Manager Frozen Decisions

1. Production backup uses the read-only, no-checkpoint path. D6 v0.1 does not add writable
   maintenance/checkpoint mode.
2. Backup and quarantine retention/pruning remain D7-owned. D6 never auto-deletes verified backups
   or restore quarantine.
3. Production restore hard-rejects SQLAlchemy manifests. D6 v0.1 has no override flag.
4. Off-ECS transfer is a documented interface only. D6 adds no executable hook, CLI option, network
   access, or mutable manifest field.
5. `BEGIN IMMEDIATE` is only a preflight signal. It must be rolled back and closed before file
   movement; stopped Compose writers plus explicit operator confirmation remain authoritative.

## 19. Implementation Sequence After Approval

1. Add shared D6 library and read-only schema validators.
2. Add backup CLI, deadline handling, immutable manifest, and generation-directory publication.
3. Add verify CLI.
4. Add restore CLI with active pointer, per-restore journals, per-file movement intents,
   reconciliation, retirement, and rollback.
5. Add unit tests, temp-dir integration tests, and Linux read-only WAL validation.
6. Update Compose profile commands and profile contract tests.
7. Update deployment and persistence runbooks, including exact old-image paired rollback.
8. Run focused D6 tests, deployment tests, full regression, Ruff, compileall, deploy-lock check, and
   `git diff --check`.

## 20. References

- Python 3.10 `sqlite3.Connection.backup(...)`: backup supports page batches, progress callback, and
  sleep between attempts, and works while other clients access the database.
- SQLite WAL read-only behavior: read-only WAL mode requires existing readable `-wal`/`-shm`, ability
  to create them, or immutable database mode. D6 forbids immutable mode against live production DBs.

This document is design-only. No scripts, Compose behavior, production data, local `.env`, real
secrets, or `data/aemeath.db` were modified in this round.
