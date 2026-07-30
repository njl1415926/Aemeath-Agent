# T-0140 Deployment Readiness v0.1 WSL Manager Review

Date: 2026-07-30

Status: **Accepted; D1 Linux acceptance gate closed**

## Environment Evidence

- WSL 2 distribution: Ubuntu 22.04.5 LTS.
- Architecture: `x86_64`; home filesystem is the WSL Linux virtual disk.
- Docker Desktop Linux engine: `linux/x86_64`, server 29.4.3.
- HTTPS access from Ubuntu worked. Docker Hub required the configured Windows proxy and showed high
  cold-connection latency, but image pulls succeeded after connection warm-up.
- No project `.env` or real provider credential was read. All runtime secrets were unique fake
  markers created only for this validation.

## Initial Findings (Resolved)

### Blocking 1: Python 3.10 conditional dependency is absent from the deployment lock

The unchanged repository image build reached the locked dependency install and failed under
`--require-hashes` because `anyio==4.14.2` requires `exceptiongroup>=1.0.2` on Python 3.10, while
`requirements.deploy.lock` contains no pinned and hashed `exceptiongroup` entry.

Required correction:

- Regenerate or amend the deployment lock for CPython 3.10 on Linux, including the applicable
  `exceptiongroup` marker, pin, and wheel hash.
- Add a target-environment gate that actually installs the lock with Python 3.10 Linux and
  `--only-binary=:all: --require-hashes`; direct-name and PEP 440 checks alone cannot prove the
  transitive marker closure.

### Blocking 2: Source code is not importable on the declared Python 3.10 target

The temporary probe image reached production preflight and then failed while importing
`app.utils.ids`: `datetime.UTC` is unavailable before Python 3.11. The same API is also used in
`app.schemas`.

Required correction:

- Preferred narrow fix: use `datetime.timezone.utc` in both files and preserve the declared Python
  3.10 target.
- Alternatively, raise the complete project and deployment baseline to Python 3.11 or newer; this
  would require coordinated changes to `requires-python`, Ruff target, Docker digest, lock, tests,
  and documentation, so it is not recommended for the current small release.
- Add a Python 3.10 container import/preflight test so higher-version Windows tests cannot hide this
  incompatibility.

## Temporary Probe Evidence

To discover downstream failures without modifying repository delivery files, Manager created a
temporary WSL-only probe with exactly two changes: a pinned/hashed `exceptiongroup` entry and
`timezone.utc` in the two incompatible source files. This probe is diagnostic only and is not an
accepted release image.

With those temporary corrections, the probe passed:

- linux/amd64 image build and image metadata inspection;
- default `10001:10001` runtime identity;
- production preflight checks for configuration, immutable resources, state volumes, file secrets,
  and trusted proxy;
- read-only root filesystem, writable `/tmp` tmpfs, writable data/log volumes;
- secret ownership `root:10001`, mode `0440`, readable but not writable by the runtime identity;
- `/health` and `/ready` HTTP 200 with `Cache-Control: no-store`;
- missing auth HTTP 401 and fake Bearer session-list HTTP 200;
- absence of pip, setuptools, wheel, compilers, GPU libraries, `.env`, DB/log artifacts, and fake
  secret markers from the image;
- SIGTERM shutdown in under one second with exit code 0 and no OOM kill.

## Final Revalidation

The Deployment Worker corrected both blockers in the repository:

- `requirements.deploy.lock` now includes the pinned and hashed Python 3.10 conditional dependency
  `exceptiongroup==1.3.1 ; python_version < "3.11"`, and the lock contract includes a missing-entry
  regression test.
- `app.utils.ids` and `app.schemas` now use `datetime.timezone.utc`, preserving Python 3.10
  compatibility without changing timestamp semantics.

Manager then repeated D1 against the unchanged working tree and official Dockerfile, without the
temporary probe:

- full regression: `584 passed, 1 skipped`; Ruff, compileall, deployment-lock validation, and
  `git diff --check` passed;
- no-cache `linux/amd64` image build succeeded and installed the complete hash-locked dependency set
  under Python 3.10, including `exceptiongroup`;
- image metadata reported Python 3.10.20, `linux/amd64`, UID/GID `10001:10001`, the approved startup
  command, and a 77,542,650-byte image;
- the final image contained no pip/setuptools/wheel, compiler, local DB/log/dotenv artifact, or local
  BGE/GPU runtime;
- production preflight passed with a read-only root filesystem, writable `/tmp` tmpfs, writable
  data/log mounts, and root-owned group-readable `0440` file secrets;
- `/health` and `/ready` returned HTTP 200 with `Cache-Control: no-store`; missing auth returned 401
  and the mounted fake Bearer token returned 200 for session listing;
- unique fake DeepSeek and Bearer markers were absent from the image, JSONL logs, and container
  stdout/stderr;
- SIGTERM shutdown completed in approximately 620 ms with exit code 0 and no OOM kill.

No real Provider call was made and no project `.env` was read or modified.

## Acceptance Decision

- D1 is **Accepted**. The Python 3.10 Linux image gate is closed.
- D2 and D3 remain accepted and consistent with their existing contracts.
- T-0140 remains `pending` because D4-D8 are not complete. D4 may now begin; D5 remains separately
  gated and is not authorized by this acceptance.
