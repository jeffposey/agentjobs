import { describe, expect, it } from "vitest";

import type { TaskRead } from "../api/types";
import { applyMove, bandMembers, describeMove, stepMove } from "./queueOrder";

function task(id: string, queue_position: number | null, priority = "high"): TaskRead {
  return {
    schema: 2,
    id,
    title: `Title of ${id}`,
    created: "2026-08-13T08:00:00Z",
    updated: "2026-08-13T09:00:00Z",
    lifecycle: queue_position === null ? "closed" : "ready",
    ball: "agent",
    ball_reason: "available",
    display_status: "Ready",
    priority,
    category: "general",
    queue_position,
    spec: { summary: `Summary of ${id}`, description: "Body." },
  } as TaskRead;
}

/** The list as a server sends it: open work by band and position, closed behind it. */
const backlog = [
  task("task-a", 100),
  task("task-b", 200),
  task("task-c", 300),
  task("task-m", 100, "medium"),
  task("task-done", null),
];

const ids = (tasks: Array<TaskRead>) => tasks.map((entry) => entry.id);

describe("stepMove", () => {
  it("steps within the band, never past its edges", () => {
    expect(stepMove(backlog, "task-b", "up")).toEqual({ before: "task-a" });
    expect(stepMove(backlog, "task-b", "down")).toEqual({ after: "task-c" });
    expect(stepMove(backlog, "task-a", "up")).toBeNull();
    expect(stepMove(backlog, "task-c", "down")).toBeNull();
  });

  it("refuses a move that would change nothing rather than firing a no-op", () => {
    // A move that lands a task exactly where it already is still writes a queue_move
    // entry, which is a record of a decision nobody made.
    expect(stepMove(backlog, "task-a", "top")).toBeNull();
    expect(stepMove(backlog, "task-c", "bottom")).toBeNull();
    expect(stepMove(backlog, "task-c", "top")).toEqual({ top: true });
  });

  it("steps past the next task in the band, not the next row on screen", () => {
    // task-m is a medium task sitting between two highs in the rendered list -- which
    // happens whenever the list is grouped by parent. Alt+Down on the high above it
    // must reach the next *high*, because a position means nothing across bands.
    const interleaved = [task("task-a", 100), task("task-m", 100, "medium"), task("task-c", 300)];
    expect(stepMove(interleaved, "task-a", "down")).toEqual({ after: "task-c" });
  });

  it("has nothing to say about a closed task", () => {
    expect(stepMove(backlog, "task-done", "up")).toBeNull();
    expect(bandMembers(backlog, "high").map((entry) => entry.id)).toEqual([
      "task-a",
      "task-b",
      "task-c",
    ]);
  });
});

describe("applyMove", () => {
  it("moves one task and leaves every other row where the server put it", () => {
    const moved = applyMove(backlog, "task-c", { top: true });

    expect(ids(moved)).toEqual(["task-c", "task-a", "task-b", "task-m", "task-done"]);
    // The band's own numbers, dealt back out in the new order -- not renumbered, and
    // not invented.
    expect(moved.slice(0, 3).map((entry) => entry.queue_position)).toEqual([100, 200, 300]);
  });

  it("does not disturb another band, or closed work", () => {
    const moved = applyMove(backlog, "task-a", { bottom: true });

    expect(ids(moved)).toEqual(["task-b", "task-c", "task-a", "task-m", "task-done"]);
    expect(moved.find((entry) => entry.id === "task-m")?.queue_position).toBe(100);
    expect(moved.find((entry) => entry.id === "task-done")?.queue_position).toBeNull();
  });

  it("places before and after relative to the named task", () => {
    expect(ids(applyMove(backlog, "task-a", { after: "task-b" }))).toEqual([
      "task-b",
      "task-a",
      "task-c",
      "task-m",
      "task-done",
    ]);
    expect(ids(applyMove(backlog, "task-c", { before: "task-b" }))).toEqual([
      "task-a",
      "task-c",
      "task-b",
      "task-m",
      "task-done",
    ]);
  });
});

describe("describeMove", () => {
  it("says what happened, since a step is often invisible on a filtered list", () => {
    expect(describeMove(backlog, "task-b", { before: "task-a" })).toBe(
      "task-b moved ahead of task-a in the high band.",
    );
    expect(describeMove(backlog, "task-b", { bottom: true })).toBe(
      "task-b moved to the bottom of the high band.",
    );
  });
});
