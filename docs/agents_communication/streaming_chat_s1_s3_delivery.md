# T-0143 Streaming Chat S1/S3 Delivery

Date: 2026-08-04

Status: **Implemented / Awaiting Manager Checkpoint**

## Delivery Scope

This delivery implements only the Manager-authorized S1 and S3 slices:

- S1: characterization tests and a shared `DialogueTurnEngine`;
- S3: provider-neutral streaming model contracts, a mock streaming client, and the DeepSeek SSE
  parser/runtime foundation.

It does not implement persistence changes, `AgentStreamService`, an SSE HTTP endpoint,
`ActiveStreamRegistry`, or any S4-S7 work. It does not modify `frontend/`, enable the production Web
Profile, or change the T-0143 task status.

## S1: Shared Dialogue Turn Engine

`DialogueController` remains the synchronous compatibility facade. Its public
`handle_user_message()` contract is unchanged, and the existing CLI and synchronous `/v1/chat`
continue to call the same method.

Shared turn preparation and decision logic now lives in `DialogueTurnEngine`:

1. preflight: Character lookup/fallback, Session validation, run creation, and `turn_started` trace;
2. Character runtime-context preparation;
3. recent-message, relationship, Semantic, Memory, and Evidence collection;
4. prompt-context construction, context-budget trace, and prompt compilation;
5. response-generated trace;
6. state and memory decisions;
7. terminal trace, prompt-run logging, and output construction.

The synchronous Controller retains ownership of the existing persistence order and semantics. It
saves the user message at the same point as before, invokes the synchronous `ChatModelClient`, then
performs the existing assistant-message, relationship, memory, trace, and prompt-log operations in
their characterized order. No second streaming Controller or duplicate orchestration path was
introduced.

Compatibility properties on `DialogueController` delegate its previous injectable dependencies to
the engine. Existing runtime wiring and tests that replace those dependencies therefore keep their
previous behavior.

### Characterized Order

The new tests fix the successful-path order across:

- Character profile and runtime context;
- Session validation;
- user-message persistence;
- recent messages and relationship state;
- Semantic Query, Memory, and Evidence;
- prompt-context construction and compilation;
- synchronous model generation;
- assistant-message persistence;
- state decision and relationship update;
- memory decision/persistence/enqueue;
- Trace Log and Prompt Log terminal records.

They also fix the Session-failure boundary, model-failure terminal logging, and content-safe DTO
representations. A static assertion prevents introduction of a `StreamingDialogueController`.

## S3: Streaming Model Foundation

The provider-neutral contract adds:

- `ModelStreamRequest`;
- `ModelTextDelta`;
- `ModelStreamFinished` with `complete` or `output_limit` finish kinds;
- `ModelStreamEvent`;
- closeable/cancellable `ModelStreamSession`;
- `StreamingChatModelClient.open_stream()`.

Request messages and delta text are excluded from object representations. The existing synchronous
`ChatModelClient` and `DeepSeekClient.chat()` path remain compatible and retain their prior payload
and retry behavior.

`MockStreamingChatModelClient` supports deterministic event sequences, injected partial-stream
failures, cancellation, and idempotent close for later service tests.

### DeepSeek Finish Semantics

| Provider input | Domain result |
| --- | --- |
| `finish_reason=stop` | `ModelStreamFinished(complete)` |
| `finish_reason=length` | `ModelStreamFinished(output_limit)` |
| `finish_reason=content_filter` | safe, non-retryable `MODEL_INVALID_RESPONSE` |
| `finish_reason=tool_calls` or tool-call delta | safe, non-retryable `MODEL_INVALID_RESPONSE` |
| `finish_reason=insufficient_system_resource` | safe, retryable `MODEL_UNAVAILABLE` |
| unknown/missing/duplicate finish state | safe `MODEL_INVALID_RESPONSE` |
| blank or whitespace-only answer | safe `MODEL_INVALID_RESPONSE`; no successful finish |

`[DONE]` is only an SSE framing terminator. Success requires a preceding accepted
`finish_reason`, a nonblank accumulated answer, and the final `[DONE]`. EOF before `[DONE]`, `[DONE]`
before a valid finish, unexpected data after a finish, malformed events, multiple choices, or a
nonzero choice index fail closed. Only an explicit usage-only empty-choices chunk may be ignored.

Only `delta.content` becomes a `ModelTextDelta`. `reasoning_content` is ignored and is not emitted,
saved, or logged. Parser and transport failures use sanitized stable errors and never include the
provider chunk or response body.

The DeepSeek stream session owns the upstream response lifetime, maps read failures safely, and
provides idempotent `cancel()`/`close()`. An injected shared `httpx.Client` is not closed by an
individual stream. Eligible connect/status retries occur only before a streaming response is
accepted; no retry occurs after HTTP 200 or after stream consumption begins.

## Files Changed

- `app/agent/dialogue_controller.py`
- `app/agent/turn_engine.py`
- `app/model/__init__.py`
- `app/model/deepseek.py`
- `app/model/mock.py`
- `app/model/mock_streaming.py`
- `app/model/streaming.py`
- `tests/test_dialogue_turn_engine.py`
- `tests/test_model_streaming.py`
- `docs/agents_communication/streaming_chat_s1_s3_delivery.md`

Unrelated pre-existing worktree changes were preserved.

## Verification

| Check | Result |
| --- | --- |
| Focused S1/S3 and compatibility suite | PASS: 84 tests |
| Full `pytest` | PASS: 683 collected, 682 passed, 1 skipped |
| `ruff check app tests` | PASS |
| `python -m compileall -q app tests` | PASS |
| `ruff check .` | BLOCKED by pre-existing, out-of-scope `frontend/capture_frontend.py:17:1 I001` |
| `git diff --check` | PASS |

The full pytest total was independently counted from pytest's grouped collection output. The test
run exited successfully and displayed one skip. No live DeepSeek request was made; provider behavior
is verified with deterministic mocked HTTP/SSE streams.

The repository-wide Ruff failure was not repaired or suppressed because this task explicitly
forbids modifying `frontend/`. All authorized application and test paths pass Ruff.

## Deferred and Checkpoint Notes

- Mapping `output_limit` to the final streaming turn warning/event belongs to the later
  `AgentStreamService` slice.
- Durable idempotency, narrow completion UoW, and schema/repository work remain outside this
  delivery.
- Admission handles, producer-to-async bridge, `ActiveStreamRegistry`, SSE API behavior, HTTP
  disconnect propagation, and Caddy integration remain S4-S7 work.
- The shared engine exposes preparation and decision boundaries for later streaming orchestration,
  but this delivery does not start a streamed turn or alter synchronous partial-persistence
  semantics.

Manager checkpoint is requested for S1/S3. No further T-0143 implementation is authorized by this
delivery.
