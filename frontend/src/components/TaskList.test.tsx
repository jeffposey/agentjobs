import { fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { describe, expect, it } from "vitest";

import type { BrokenTaskFile, TaskRead } from "../api/generated";
import { TaskList } from "./TaskList";

function task(id: string, overrides: Partial<TaskRead> = {}): TaskRead {
  return {
    schema: 2,
    id,
    title: `Title of ${id}`,
    created: "2026-08-13T08:00:00Z",
    updated: "2026-08-13T09:00:00Z",
    lifecycle: "ready",
    ball: "agent",
    ball_reason: "available",
    display_status: "Ready",
    priority: "medium",
    category: "general",
    tags: [],
    assignment: { eligible: [] },
    spec: { summary: `Summary of ${id}`, description: "Body." },
    ...overrides,
  };
}

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="location">{location.pathname}{location.search}</output>;
}

function renderList(tasks: Array<TaskRead>, entry = "/p/inbox/tasks", brokenFiles: Array<BrokenTaskFile> = []) {
  render(
    <MemoryRouter initialEntries={[entry]}>
      <TaskList tasks={tasks} brokenFiles={brokenFiles} projectId="inbox" />
      <LocationProbe />
    </MemoryRouter>,
  );
}

describe("TaskList filtering", () => {
  it("defaults to Open and excludes closed tasks", () => {
    renderList([
      task("task-open"),
      task("task-closed", { lifecycle: "closed", ball: null, ball_reason: null, outcome: "completed", display_status: "Completed" }),
    ]);

    expect(screen.getByRole("combobox", { name: "Status" })).toHaveValue("open");
    expect(screen.getByText("task-open")).toBeVisible();
    expect(screen.queryByText("task-closed")).not.toBeInTheDocument();
  });

  it("flattens filtered results so a child survives a filtered-out parent", () => {
    renderList([
      task("task-parent", { lifecycle: "closed", ball: null, ball_reason: null, outcome: "completed", display_status: "Completed" }),
      task("task-child", { parent: "task-parent" }),
    ]);

    expect(screen.queryByText("task-parent")).not.toBeInTheDocument();
    expect(screen.getByText("task-child")).toBeVisible();
    expect(screen.getByText(/part of task-parent/)).toBeVisible();
  });

  it("renders dangling parents and every task in a parent cycle", () => {
    renderList([
      task("task-dangling", { parent: "task-missing" }),
      task("task-cycle-a", { parent: "task-cycle-b" }),
      task("task-cycle-b", { parent: "task-cycle-a" }),
    ], "/p/inbox/tasks?status=all");

    const table = screen.getByRole("region", { name: "Tasks" });
    expect(within(table).getByText("task-dangling")).toBeVisible();
    expect(within(table).getByText("task-cycle-a")).toBeVisible();
    expect(within(table).getByText("task-cycle-b")).toBeVisible();
  });

  it("writes every filter to the URL and restores it from a refreshed URL", () => {
    renderList([task("task-high", { priority: "high", tags: ["test"] })]);

    fireEvent.change(screen.getByRole("searchbox", { name: "Search tasks" }), { target: { value: "high" } });
    fireEvent.change(screen.getByRole("combobox", { name: "Status" }), { target: { value: "all" } });
    fireEvent.change(screen.getByRole("combobox", { name: "Priority" }), { target: { value: "high" } });
    fireEvent.change(screen.getByRole("combobox", { name: "Scope" }), { target: { value: "test" } });

    const url = screen.getByTestId("location").textContent ?? "";
    expect(url).toContain("q=high");
    expect(url).toContain("status=all");
    expect(url).toContain("priority=high");
    expect(url).toContain("scope=test");
  });

  it("surfaces unreadable task files above the list", () => {
    renderList([], "/p/inbox/tasks", [{
      task_id: "task-broken",
      path: "C:/project/tasks/task-broken.yaml",
      filename: "task-broken.yaml",
      reason: "schema: Input should be 2",
    }]);

    const warning = screen.getByRole("region", { name: "Unreadable task files" });
    expect(warning).toHaveTextContent("task-broken.yaml — schema: Input should be 2");
  });

  it("explains dependency blocks in words on the task row", () => {
    renderList([task("task-blocked", {
      actionable: false,
      unmet_needs: ["task-prerequisite (still open)"],
    })]);

    const rows = screen.getByRole("region", { name: "Tasks" });
    expect(within(rows).getByText("Blocked")).toBeVisible();
    expect(within(rows).getByText("Waiting for task-prerequisite (still open)")).toBeVisible();
  });

  it("explains why an umbrella is not actionable", () => {
    renderList([task("task-umbrella", {
      actionable: false,
      open_children_count: 2,
    })]);

    expect(screen.getByText("Waiting on sub-tasks")).toBeVisible();
    expect(screen.getByText("2 open sub-tasks must finish first.")).toBeVisible();
  });
});
