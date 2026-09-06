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

describe("DependencyState in a list column", () => {
  const blocked: TaskRead = {
    schema: 2,
    id: "task-341-fixture",
    title: "A task waiting on two others",
    created: "2026-09-05T08:00:00Z",
    updated: "2026-09-05T09:00:00Z",
    lifecycle: "ready",
    ball: "agent",
    ball_reason: "work",
    outcome: null,
    display_status: "Ready",
    priority: "medium",
    category: "ux",
    tags: [],
    unmet_needs: ["task-042", "task-043"],
    assignment: { eligible: [] },
    spec: { summary: "Summary.", description: "Body." },
  };

  // A `ball_prompt` is written for someone who has the task open, so it is routinely
  // several paragraphs. Rendered one `<p>` per reason in a 194px table column it made
  // one row 2091px tall and emptied the first screen of the task list (task-341).
  it("puts every reason in one paragraph so the column can clamp it", () => {
    const { container } = render(<DependencyState task={blocked} compact />);

    const paragraphs = container.querySelectorAll("p");
    expect(paragraphs).toHaveLength(1);
    expect(paragraphs[0]).toHaveTextContent("Waiting for task-042 · Waiting for task-043");
  });

  // Clamped, not truncated: the cut is CSS, and the whole text is still on the element
  // for a reader who hovers it. Nothing is dropped from the DOM.
  it("keeps the full text reachable on the element that shows the clamp", () => {
    const { container } = render(<DependencyState task={blocked} compact />);

    const paragraph = container.querySelector("p");
    expect(paragraph).toHaveAttribute(
      "title",
      "Waiting for task-042 · Waiting for task-043",
    );
  });

  // The task's own page has room for the whole thing, and is where a reader who wants
  // to act on it is going. Only the list summarises.
  it("still gives each reason its own paragraph when it is not compact", () => {
    const { container } = render(<DependencyState task={blocked} />);

    expect(container.querySelectorAll("p")).toHaveLength(2);
    expect(container.querySelector("p")).not.toHaveAttribute("title");
  });

  it("renders no paragraph at all when there is nothing to say", () => {
    const quiet = { ...blocked, unmet_needs: [], actionable: true };
    const { container } = render(<DependencyState task={quiet} compact />);

    expect(container.querySelectorAll("p")).toHaveLength(0);
    expect(screen.getByText("Actionable now")).toBeVisible();
  });
});
