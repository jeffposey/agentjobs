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
