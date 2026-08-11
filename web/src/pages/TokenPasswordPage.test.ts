import { describe, expect, it } from "vitest";

import { clearSensitiveFragment, readFragmentToken } from "./TokenPasswordPage";

describe("readFragmentToken", () => {
  it("reads a named token without putting it in the query string", () => {
    expect(readFragmentToken("#token=invite_secret")).toBe("invite_secret");
  });

  it("supports a bare fragment token", () => {
    expect(readFragmentToken("#reset_secret")).toBe("reset_secret");
  });

  it("rejects fragments without a token", () => {
    expect(readFragmentToken("#source=email")).toBeNull();
  });
});

describe("clearSensitiveFragment", () => {
  it("removes the credential fragment while preserving path and query", () => {
    window.history.replaceState({}, "", "/invite?source=email#token=invite_secret");

    clearSensitiveFragment();

    expect(window.location.pathname).toBe("/invite");
    expect(window.location.search).toBe("?source=email");
    expect(window.location.hash).toBe("");
  });
});
