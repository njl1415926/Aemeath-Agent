# T-0143 Streaming Chat S6 Delivery

Date: 2026-08-06

Status: **S6 Accepted by Manager / T-0143 In Progress**

Baseline: `44eddf3`

## Scope

This delivery implements only the authorized S6 observability, post-header error-boundary, and
Caddy targeted-test work. It does not enter S7, change persistence schema, change synchronous CLI or
`/v1/chat`, modify `frontend/`, enable the production Web Profile, or mark T-0143 done.

## Post-header ASGI Error Boundary

`OwnedStreamingResponse` now treats both raw `OSError` and Starlette `ClientDisconnect` raised by
its direct ASGI send path as `client_disconnected`. The send wrapper establishes the existing
first-wins cancellation intent before the async generator can classify its close, then the existing
bridge-abort and off-loop `cancel() + close()` lifecycle releases the Provider response, LLM gate,
dialogue admission, producer, and active-stream registration.

FastAPI's function middleware is implemented by Starlette `BaseHTTPMiddleware`. Its body forwarding
can call the server's real `send()` after the inner `OwnedStreamingResponse` send has succeeded, so
the response owner alone cannot catch every downstream exception. S6 therefore adds a narrow pure
ASGI `SSEClientDisconnectMiddleware` for `/v1/chat/stream` only. It:

- starts suppressing send failures only after `http.response.start` was successfully sent;
- catches only downstream `OSError` and `ClientDisconnect`;
- notifies the response owner through request-scoped state;
- freezes `client_disconnected`, aborts the bridge, and awaits off-loop idempotent handle cleanup;
- suppresses the private post-header exception instead of allowing a traceback or proxy 502;
- leaves pre-header failures and every non-stream route on the existing HTTP error boundary.

The request-scoped owner reference is removed by response finalization. Repeated notification from
the direct response path, middleware path, generator finalizer, or response finalizer remains safe.

If cancellation wins before completion CAS, the durable failed reason is `client_disconnected`, the
partial accumulator is discarded, and no assistant message is stored. If completion CAS wins first,
the later delivery failure does not rewrite the durable completed record or assistant message.

## Observability and Error Safety

The adapter records only safe identifiers, event names, status, and exception class. It never stores
or logs `str(exc)`. New marker tests cover successful and failed streams and inspect App Log, Trace,
Prompt Summary, public terminal objects, and safe `repr` output for absence of:

- user message and compiled prompt content;
- response delta and accumulated partial response;
- credentials and authorization fragments;
- Memory/RAG-style vector content;
- Provider raw response/body markers;
- raw exception-body markers.

Streaming Prompt Log remains summary-only and best-effort: `messages=[]`, `sections=[]`,
`response=None`, `error=None`. Failure records retain only stable code/type/public-message and
bounded counters. The ASGI send-failure marker is also absent from API and stream App Logs.

## Caddy Targeted Tests

`deploy/Caddyfile` was not changed. Static contract coverage now explicitly rejects any
`flush_interval` directive while retaining `protocols h1 h2`, exact `128KiB` raw request-body limit,
first-hop forwarding defaults, disabled access log, and no h3 advertisement.

The optional real-binary test uses the pinned Caddy 2.10.2 release and a synthetic streaming
upstream. It verifies:

- an HTTP/1.1 delta arrives before the upstream is allowed to send its terminal;
- a heartbeat and terminal arrive as separate streamed content;
- a real HTTP/2 request negotiates HTTP/2 and receives the same live delta, heartbeat, and terminal;
- downstream close is observed by the upstream within a bounded deadline;
- exactly 128 KiB is accepted and 128 KiB + 1 is rejected with 413;
- direct ALPN negotiation accepts both `http/1.1` and `h2`;
- `Alt-Svc` does not advertise h3.

The integration fixture copies the production Caddyfile and adds only test-process isolation:
an ephemeral admin listener and `skip_install_trust`. This prevents concurrent/back-to-back tests
from sharing Caddy's default admin port or modifying the workstation trust store; routing, protocol,
body-limit, and reverse-proxy directives remain the production ones.

`h2>=4.1,<5` is added only to the development dependency group so `httpx` can issue a real HTTP/2
test request. It is not added to the deployment image/runtime lock.

## Files Changed in S6

Modified:

- `app/api/sse.py`: direct-send classification, response-owner disconnect notification, and the
  narrow pure-ASGI downstream disconnect boundary;
- `app/api/server.py`: middleware composition;
- `tests/test_api_streaming.py`: direct and full-app ASGI 2.4 send-fault tests plus durable-terminal
  race coverage;
- `tests/test_agent_stream_service.py`: successful/failed observability non-leakage coverage;
- `tests/deployment/test_ingress_contract.py`: no-`flush_interval` contract;
- `tests/deployment/test_caddy_body_limit_integration.py`: isolated Caddy test process and portable
  IPv4/IPv6 loopback assertion;
- `pyproject.toml`: test-only HTTP/2 support in the development extra.

Added:

- `tests/deployment/test_caddy_streaming_integration.py`;
- `docs/agents_communication/streaming_chat_s6_delivery.md`.

No persistence, Controller, model-provider, Caddyfile, Compose, frontend, production-profile, or
Manager-owned review/task-status document was changed by S6.

## Fault and Regression Tests

- direct `OwnedStreamingResponse` ASGI 2.4 `ClientDisconnect`: no exception escape, durable
  `client_disconnected`, no assistant, and zero admission/gate/registry ownership;
- full FastAPI middleware-chain raw send `OSError`: same assertions plus no exception-body marker in
  App Logs;
- send failure while delivering an already committed terminal: durable completed and assistant
  message remain unchanged;
- existing request-disconnect, generator-close, producer-start failure, blocking transport cancel,
  capacity-one bridge, consumer-slow, replay, Auth/CSRF/rate-limit, shutdown, and sync regressions;
- successful and failed stream log marker scans across App Log, Trace, Prompt Summary, public errors,
  and safe repr;
- real Caddy h1/h2 live streaming, heartbeat, disconnect, body-limit, forwarding, and protocol tests.

## Verification

| Check | Result |
| --- | --- |
| Expanded S1-S6/API/Auth/runtime/store/Caddy suite | PASS: 204 tests |
| Full `pytest -q` with pinned Caddy 2.10.2 enabled | PASS: 805 tests |
| Real Caddy body/SSE suite repeated | PASS |
| `python -m ruff check app tests` | PASS |
| `python -m compileall -q app tests` | PASS |
| `git diff --check` | PASS |
| `python -m ruff check .` | BLOCKED only by pre-existing forbidden `frontend/capture_frontend.py:17:1 I001` |

The focused and full suites use fresh writable local `--basetemp` directories. No real DeepSeek,
browser UI, production certificate, public DNS, or multi-worker deployment was exercised.

## Risks and Deferred Before S7

1. The real proxy test uses localhost TLS and a deterministic synthetic upstream; public-network,
   browser, load-balancer, and production certificate behavior remains outside S6.
2. The pure-ASGI boundary intentionally suppresses only post-header send disconnects on
   `/v1/chat/stream`. Other send failures and pre-header failures retain existing global behavior.
3. Cancellation and registries remain single-process; production must retain one Uvicorn worker.
4. Frontend Stop/Retry UX, long-session browser soak, and production Web Profile activation remain
   deferred.

S6 stops here for the Manager checkpoint. S7 remains blocked and T-0143 remains `in_progress`.
