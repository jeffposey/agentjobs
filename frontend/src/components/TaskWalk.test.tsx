import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import type { EpicWalkView } from "../api/types";
import { TaskWalkPanel, walkBadge, walkForTask } from "./TaskWalk";

function walk(overrides: Partial<EpicWalkView> = {}): EpicWalkView {
  return {
    walk_id: "walk_aaaa",
    project_id: "alpha",
    project_name: "Alpha Project",
    parent_task_id: "task-555",
    parent_task_title: "Design tasks look different",
    parent_task_url: "/p/alpha/tasks/task-555",
    started_at: "2026-09-25T15:40:56Z",
    children_total: 2,
    children_completed: 0,
    children_in_flight: 1,
    children_remaining: 1,
    in_flight_task_ids: ["task-556"],
    grounded: false,
    grounded_reason: "",
    grounded_word: "",
    waiting_on_task_id: "",
    waiting_on_task_title: "",
    waiting_on_task_url: "",
    resumes_by_itself: false,
    detail: "",
    ...overrides,
  };
}

const taskPath = (id: string) => `/p/alpha/tasks/${id}`;

function renderPanel(value: EpicWalkView) {
  render(
    <MemoryRouter>
      <TaskWalkPanel walk={value} taskPath={taskPath} />
    </MemoryRouter>,
  );
}

describe("finding this task's walk", () => {
  it("matches on project and parent task, and nothing else", () => {
    const mine = walk();
    const walks = [
      walk({ walk_id: "other-project", project_id: "beta" }),
      walk({ walk_id: "other-task", parent_task_id: "task-437" }),
      mine,
    ];

    expect(walkForTask(walks, "alpha", "task-555")).toBe(mine);
    expect(walkForTask(walks, "alpha", "task-556")).toBeNull();
    expect(walkForTask(null, "alpha", "task-555")).toBeNull();
  });
});

describe("the walk on the epic's own page", () => {
  it("says a walking epic is being walked, with a moving Running badge, its counts and the child in flight", () => {
    renderPanel(walk());

    expect(screen.getByRole("heading", { name: "Being walked" })).toBeInTheDocument();
    // "Running", not a second "Walking": the task's own chip already says that.
    const badge = screen.getByTestId("task-walk-badge");
    expect(badge).toHaveTextContent("Running");
    expect(badge).toHaveAttribute("data-motion", "orbit");
    expect(screen.getByTestId("task-walk-counts")).toHaveTextContent(
      "0 of 2 children done · 1 in flight · 1 to come",
    );
    expect(screen.getByTestId("task-walk-state")).toHaveTextContent(
      "Starting each child as its dependencies close",
    );
    expect(screen.getByRole("link", { name: "task-556" })).toHaveAttribute(
      "href",
      "/p/alpha/tasks/task-556",
    );
  });

  it("tells a waiting walk from a walking one, and names what it waits on", () => {
    renderPanel(
      walk({
        grounded: true,
        grounded_reason: "child_needs_a_human",
        grounded_word: "a child needs a person",
        resumes_by_itself: true,
        in_flight_task_ids: [],
        children_in_flight: 0,
        waiting_on_task_id: "task-556",
        waiting_on_task_title: "Design pass",
        waiting_on_task_url: "/p/alpha/tasks/task-556",
      }),
    );

    // Stands still: nothing is being started while it waits.
    const badge = screen.getByTestId("task-walk-badge");
    expect(badge).toHaveTextContent("Waiting");
    expect(badge).not.toHaveAttribute("data-motion");
    expect(screen.getByTestId("task-walk-state")).toHaveTextContent(
      "Waiting on task-556: a child needs a person. It takes off again on its own when that clears.",
    );
    expect(screen.getByTestId("task-walk-waiting-on")).toHaveTextContent("task-556 Design pass");
    expect(screen.queryByTestId("task-walk-in-flight")).toBeNull();
  });

  it("says a grounded walk will not continue without a person", () => {
    const value = walk({
      grounded: true,
      grounded_reason: "child_closed_unresolved",
      grounded_word: "a child closed unresolved",
    });

    expect(walkBadge(value)).toBe("Grounded");
    renderPanel(value);
    expect(screen.getByTestId("task-walk-state")).toHaveTextContent(
      "Grounded: a child closed unresolved. Nothing more takes off until a person acts.",
    );
  });
});
