import { describe, expect, it } from "vitest";

import { readRefusal } from "./mutation-error";

describe("readRefusal", () => {
  it("reads the code and message the API refuses with", () => {
    // The body a 409 actually carries, as built by _error() in api/routes/status.py.
    expect(
      readRefusal({
        code: "revision_conflict",
        message: "Task 'task-004' changed since the revision you read.",
        detail: "Task 'task-004' changed since the revision you read.",
        retryable: false,
        task_id: "task-004",
        suggested_action: "Re-read the task, decide again, and resend.",
      }),
    ).toEqual({
      code: "revision_conflict",
      message: "Task 'task-004' changed since the revision you read.",
      suggestedAction: "Re-read the task, decide again, and resend.",
    });
  });

  it("falls back to detail when message is absent", () => {
    expect(readRefusal({ code: "invalid_transition", detail: "Not a draft." })?.message).toBe("Not a draft.");
  });

  it("returns null for anything that is not a structured refusal", () => {
    // A network failure, an HTML error page, a thrown string: the caller has to be
    // able to tell these from "the server refused and said why".
    expect(readRefusal(new TypeError("Failed to fetch"))).toBeNull();
    expect(readRefusal("500 Internal Server Error")).toBeNull();
    expect(readRefusal(null)).toBeNull();
    expect(readRefusal({ detail: [{ loc: ["body"], msg: "field required" }] })).toBeNull();
  });
});
