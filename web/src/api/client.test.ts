import { afterEach, describe, expect, it, vi } from "vitest";

import { parseError, subscribeAuthInvalidation } from "./client";

afterEach(() => {
  vi.restoreAllMocks();
});

describe("browser session invalidation", () => {
  it("notifies the shared auth boundary for an expired session", async () => {
    const listener = vi.fn();
    const unsubscribe = subscribeAuthInvalidation(listener);
    const response = new Response(
      JSON.stringify({
        error: {
          code: "AUTH_SESSION_EXPIRED",
          message: "Session expired.",
          retryable: false,
        },
        request_id: "req_auth_expired",
      }),
      { status: 401, headers: { "Content-Type": "application/json" } },
    );

    const error = await parseError(response);

    expect(error.code).toBe("AUTH_SESSION_EXPIRED");
    expect(listener).toHaveBeenCalledOnce();
    unsubscribe();
  });

  it("does not invalidate the session for an ordinary permission error", async () => {
    const listener = vi.fn();
    const unsubscribe = subscribeAuthInvalidation(listener);
    const response = new Response(
      JSON.stringify({
        error: {
          code: "PERMISSION_DENIED",
          message: "Permission denied.",
          retryable: false,
        },
        request_id: "req_permission",
      }),
      { status: 403, headers: { "Content-Type": "application/json" } },
    );

    await parseError(response);

    expect(listener).not.toHaveBeenCalled();
    unsubscribe();
  });
});
