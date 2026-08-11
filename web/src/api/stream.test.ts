import { describe, expect, it } from "vitest";

import { parseSseFrame } from "./stream";

describe("parseSseFrame", () => {
  it("ignores heartbeat comments", () => {
    expect(parseSseFrame(": heartbeat")).toBeNull();
  });

  it("parses unicode response deltas", () => {
    expect(
      parseSseFrame('event: response.delta\ndata: {"delta":"早哇，漂泊者。"}'),
    ).toEqual({ type: "response.delta", delta: "早哇，漂泊者。" });
  });

  it("parses the stable public failure shape", () => {
    expect(
      parseSseFrame(
        'event: turn.failed\ndata: {"turn_id":"turn_1","error":{"code":"MODEL_UNAVAILABLE","message":"暂时无法回应。","retryable":true}}',
      ),
    ).toEqual({
      type: "turn.failed",
      turn_id: "turn_1",
      error: {
        code: "MODEL_UNAVAILABLE",
        message: "暂时无法回应。",
        retryable: true,
      },
    });
  });
});
