# Changelog

## v1.0.0 - 2026-08-12

- Promoted the invitation-only Web release after the core path ran on a real Ubuntu 22.04 ECS host.
- Verified the locked non-root API/Caddy images, private API port, public DNS, trusted Let's Encrypt
  HTTPS, and public health endpoint.
- Added the production release notes and the end-to-end server bootstrap runbook.
- Kept the small-launch boundary unchanged: Memory Worker, production long-term Memory, vector RAG,
  local embedding, audio, public registration, and horizontal scaling remain disabled.
- Kept reboot, image replacement, D6 restore, and scheduled host-operation drills as explicit
  operational follow-ups before expanding the invitation group.

## v1.0.0-rc.1 - 2026-08-11

- Added the invitation-only React/Vite Web client with Cookie Session authentication and CSRF.
- Added resumable SSE streaming chat with durable idempotency and disconnect-safe cleanup.
- Added browser Session title, lifecycle, history, and opaque-cursor pagination APIs.
- Added account invitations, password management, owner-only permissions, and abuse controls.
- Added per-account daily usage quotas integrated atomically with streaming persistence.
- Added deterministic context budgeting and shared turn-level semantic query infrastructure.
- Added production Docker Compose, non-root Caddy ingress, health/readiness checks, and secret files.
- Added SQLite backup/verify/restore tooling and host log-retention/health systemd operations.
- Kept production CPU-only and single-node: local BGE-M3, Memory Worker, vector RAG, and audio remain disabled.
- Marked the release as a candidate until D8 real-ECS HTTPS, reboot, restore, and browser smoke pass.

## v0.3.0 - 2026-06-08

- Added first-class Session identity, ownership, lifecycle, and CLI management.
- Added explicit and window-triggered long-term memory extraction jobs.
- Added synchronous memory extraction and a standalone worker.
- Added hash and optional local BGE-M3 embedding recall.
- Added memory scope and provenance contracts with ownership-safe administration.
- Added versioned migration for legacy Sessions and long-term memories.
- Added Chinese encoding guardrails and behavior tests.

## v0.2.0 - 2026-06-04

- Added Character v0.2 manifests and World Info RAG.
- Added structured prompt assembly and local JSONL prompt-run logs.
- Added prompt-log inspection and early evaluation samples.
