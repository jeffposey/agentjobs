import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { TaskRead } from "../api/types";
import { DependencyState } from "./DependencyState";

function closedTask(outcome: "completed" | "cancelled" | "superseded" | "duplicate", displayStatus: string): TaskRead {
  return {
    schema: 2,
    id: `task-${outcome}`,
    title: `A ${outcome} task`,
    created: "2026-08-13T08:00:00Z",
    updated: "2026-08-13T09:00:00Z",
    lifecycle: "closed",
    ball: null,
    ball_reason: null,
    outcome,
    display_status: displayStatus,
    priority: "medium",
    category: "general",
    tags: [],
    assignment: { eligible: [] },
    spec: { summary: "Summary.", description: "Body." },
  };
}

describe("DependencyState closed outcomes", () => {
  // Every closed task used to render the single word "Done". Jeff read that on the
  // task list for task-058, whose record says Superseded, and reasonably concluded
  // the work had been finished.
  it.each([
    ["completed", "Completed"],
    ["cancelled", "Cancelled"],
    ["superseded", "Superseded"],
    ["duplicate", "Duplicate"],
  ] as const)("shows %s as its own label rather than Done", (outcome, label) => {
    render(<DependencyState task={closedTask(outcome, label)} />);

    expect(screen.getByText(label)).toBeVisible();
    expect(screen.queryByText("Done")).not.toBeInTheDocument();
  });

  it("carries the archived suffix the backend puts in display_status", () => {
    render(<DependencyState task={{ ...closedTask("superseded", "Superseded (archived)"), archived: true }} />);

    expect(screen.getByText("Superseded (archived)")).toBeVisible();
  });

  it("tints an unfinished outcome differently from a completed one", () => {
    const { unmount } = render(<DependencyState task={closedTask("completed", "Completed")} />);
    const completedClasses = screen.getByText("Completed").className;
    unmount();

    render(<DependencyState task={closedTask("superseded", "Superseded")} />);

    expect(screen.getByText("Superseded").className).not.toEqual(completedClasses);
  });
});

describe("DependencyState on a held task", () => {
  // task-231: `agent/hold` is the first agent-ball state that is not workable, so
  // every place that read "active" as "somebody is working on it" had to be revisited.
  // This badge was one of them: it fell through to `lifecycle === "active"` and read
  // "In flight" on the one task a human had deliberately stopped. Found by looking at
  // it in a browser, not by any test that existed at the time.
  const held: TaskRead = {
    schema: 2,
    id: "task-held",
    title: "A held task",
    created: "2026-08-13T08:00:00Z",
    updated: "2026-08-13T09:00:00Z",
    lifecycle: "active",
    ball: "agent",
    ball_reason: "hold",
    ball_prompt: "ON HOLD -- wait for the dispatch fixes.",
    display_status: "On hold (claude)",
    priority: "high",
    category: "general",
    tags: [],
    assignment: { owner: "claude", eligible: [] },
    spec: { summary: "Summary.", description: "Body." },
  };

  it("says it is on hold rather than in flight", () => {
    render(<DependencyState task={held} />);

    expect(screen.getByText("On hold (claude)")).toBeVisible();
    expect(screen.queryByText("In flight")).not.toBeInTheDocument();
  });

  it("says so even when the task is also blocked on an unmet dependency", () => {
    // A hold outranks the dependency: nothing moves until a person releases it,
    // whatever else is also true of the task.
    render(<DependencyState task={{ ...held, unmet_needs: ["task-042"] }} />);

    expect(screen.getByText("On hold (claude)")).toBeVisible();
    expect(screen.queryByText("Blocked")).not.toBeInTheDocument();
  });
});
