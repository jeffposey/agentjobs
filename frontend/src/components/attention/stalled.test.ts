import { describe, expect, it } from "vitest";

import type { StalledTaskRead } from "../../api/types";
import { quietPhrase, stallBadge, stallExplanation, stallsByTask } from "./stalled";

/**
 * How a stalled task is said, given what the server decided about it (task-499).
 *
 * The property worth protecting here is that every string is built from `reason` and
 * `quiet_seconds` and never from a label the server also renders: two surfaces print
 * this, and matching on the prose of one of them is what ENGINEERING.md's
 * rendered-value rule exists to prevent.
 */

function stall(overrides: Partial<StalledTaskRead> = {}): StalledTaskRead {
  return {
    task_id: "task-421",
    reason: "no_agent",
    quiet_since: "2026-09-19T21:05:00Z",
    quiet_seconds: 22 * 3600,
    threshold_seconds: 3600,
    run_id: "",
    ...overrides,
  };
}

describe("quietPhrase", () => {
  it("prints minutes below an hour", () => {
    expect(quietPhrase(61 * 60)).toBe("1h 1m");
    expect(quietPhrase(45 * 60)).toBe("45m");
  });

  it("never prints seconds, because this is an hours-scale signal", () => {
    expect(quietPhrase(90)).toBe("1m");
    expect(quietPhrase(0)).toBe("0m");
  });

  it("does not go negative on a clock that disagrees with the server's", () => {
    expect(quietPhrase(-500)).toBe("0m");
  });
});

describe("what the row says", () => {
  it("names the absence when nothing is running against the task", () => {
    expect(stallBadge(stall())).toBe("No agent for 22h 0m");
    expect(stallExplanation(stall())).toContain("nothing has been running against it");
  });

  it("names the run when feedback was queued for one that never moved", () => {
    const undelivered = stall({
      reason: "undelivered_handback",
      run_id: "run_9df54f12",
      quiet_seconds: 40 * 60,
    });

    expect(stallBadge(undelivered)).toBe("Feedback undelivered 40m");
    expect(stallExplanation(undelivered)).toContain("run_9df54f12");
  });

  it("does not repeat the ball prompt, which is addressed to an agent that is gone", () => {
    expect(stallExplanation(stall())).toMatch(/Re-dispatch it, take it over, or leave it/);
  });
});

describe("stallsByTask", () => {
  it("is empty for a project where nothing has been abandoned", () => {
    expect(stallsByTask([]).size).toBe(0);
    expect(stallsByTask(undefined).size).toBe(0);
  });

  it("keys the stalls by the task each is about", () => {
    const map = stallsByTask([stall(), stall({ task_id: "task-176" })]);

    expect([...map.keys()].sort()).toEqual(["task-176", "task-421"]);
  });
});
