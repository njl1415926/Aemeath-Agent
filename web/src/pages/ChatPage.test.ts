import { describe, expect, it } from "vitest";

import { shouldClearErrorAfterStream } from "./ChatPage";

describe("stream terminal error visibility", () => {
  it("preserves the public error while failed-turn history is refreshed", () => {
    expect(shouldClearErrorAfterStream("failed")).toBe(false);
  });

  it("clears stale errors after successful completion", () => {
    expect(shouldClearErrorAfterStream("completed")).toBe(true);
  });
});
