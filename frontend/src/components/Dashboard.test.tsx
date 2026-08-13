import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import type { DashboardResponse, TaskRead } from "../api/generated";
import { Dashboard } from "./Dashboard";

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
    spec: { summary: `Summary of ${id}`, description: "Body." },
    ...overrides,
  };
}

const blocked = task("task-blocked", {
  lifecycle: "active",
  ball: "human",
  ball_reason: "review",
  ball_prompt: "Approve or request changes.",
  display_status: "Waiting for review",
});
const backlog = task("task-backlog", {
  lifecycle: "draft",
  ball: "human",
  ball_reason: "spec",
  ball_prompt: "Decide whether this should become work.",
  display_status: "Draft",
});
const claimable = task("task-next");

function dashboard(overrides: Partial<DashboardResponse>): DashboardResponse {
  return {
    stats: {
      total: 1,
      in_progress: 0,
      blocked: 0,
      waiting_for_human: 0,
      awaiting_input: 0,
      completed: 0,
    },
    active_tasks: [],
    recent_updates: [],
    waiting_tasks: [],
    backlog_tasks: [],
    next_task: null,
    next_action: "nothing_claimable",
    broken_files: [],
    ...overrides,
  };
}

function renderDashboard(value: DashboardResponse) {
  render(
    <MemoryRouter>
      <Dashboard dashboard={value} projectId="inbox" />
    </MemoryRouter>,
  );
}

describe("Dashboard next-action ladder", () => {
  const cases: Array<{
    name: string;
    response: DashboardResponse;
    exactHeading: string;
  }> = [
    {
      name: "blocked",
      response: dashboard({
        next_action: "blocked",
        waiting_tasks: [blocked],
        stats: {
          total: 3,
          in_progress: 0,
          blocked: 0,
          waiting_for_human: 1,
          awaiting_input: 1,
          completed: 0,
        },
      }),
      exactHeading: "1 Task Blocked on You",
    },
    {
      name: "backlog",
      response: dashboard({ next_action: "backlog", backlog_tasks: [backlog] }),
      exactHeading: "Backlog awaiting your input (1)",
    },
    {
      name: "next up",
      response: dashboard({ next_action: "next_up", next_task: claimable }),
      exactHeading: "Next up",
    },
    {
      name: "nothing claimable",
      response: dashboard({ next_action: "nothing_claimable" }),
      exactHeading: "Nothing claimable right now",
    },
    {
      name: "empty project",
      response: dashboard({
        next_action: "empty_project",
        stats: {
          total: 0,
          in_progress: 0,
          blocked: 0,
          waiting_for_human: 0,
          awaiting_input: 0,
          completed: 0,
        },
      }),
      exactHeading: "Getting Started with AgentJobs",
    },
  ];

  for (const { name, response, exactHeading } of cases) {
    it(`renders exactly the ${name} call to action`, () => {
      renderDashboard(response);

      const action = screen.getByTestId("next-action");
      expect(within(action).getByRole("heading", { name: exactHeading })).toBeVisible();
      expect(screen.getAllByTestId("next-action")).toHaveLength(1);
      for (const other of cases.filter((candidate) => candidate.exactHeading !== exactHeading)) {
        expect(within(action).queryByRole("heading", { name: other.exactHeading })).not.toBeInTheDocument();
      }
    });
  }

  it("keeps the backlog count linked while the blocked panel suppresses the backlog panel", () => {
    renderDashboard(cases[0]!.response);

    expect(screen.getByRole("link", { name: "+1 in backlog" })).toHaveAttribute(
      "href",
      "/p/inbox/tasks?status=draft",
    );
    expect(within(screen.getByTestId("next-action")).queryByText(/Backlog awaiting your input/)).not.toBeInTheDocument();
  });

  it("explains a dependency block on an active task card", () => {
    renderDashboard(dashboard({
      active_tasks: [task("task-waiting", {
        actionable: false,
        unmet_needs: ["task-first (still open)"],
      })],
    }));

    expect(screen.getByText("Waiting for task-first (still open)")).toBeVisible();
  });

  it("keeps a human-held active card in review instead of calling it in flight", () => {
    renderDashboard(dashboard({ active_tasks: [blocked] }));

    expect(screen.getByText("Waiting for review")).toBeVisible();
    expect(screen.queryByText("In flight")).not.toBeInTheDocument();
  });

  it.each(["nothing_claimable", "empty_project"] as const)(
    "links the %s ladder rung to browser task creation",
    (nextAction) => {
      renderDashboard(dashboard({ next_action: nextAction }));

      expect(screen.getByRole("link", { name: "Create task" })).toHaveAttribute(
        "href",
        "/p/inbox/tasks/new",
      );
    },
  );
});

describe("Dashboard supporting sections", () => {
  it("keeps review actions off the dashboard", () => {
    renderDashboard(dashboard({ waiting_tasks: [blocked], next_action: "blocked" }));

    expect(screen.queryByRole("button", { name: /Approve/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Request Changes/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Reject/ })).not.toBeInTheDocument();
  });

  it("keeps task statistics in one compact semantic summary", () => {
    renderDashboard(dashboard({
      stats: {
        total: 12,
        in_progress: 2,
        blocked: 1,
        waiting_for_human: 3,
        awaiting_input: 4,
        completed: 6,
      },
    }));

    const statistics = screen.getByRole("region", { name: "Task statistics" });
    expect(within(statistics).getAllByRole("definition")).toHaveLength(5);
    expect(within(statistics).getByRole("link", { name: "+4 in backlog" })).toHaveAttribute(
      "href",
      "/p/inbox/tasks?status=draft",
    );
  });

  it("surfaces unreadable task files with their exact filename and reason", () => {
    renderDashboard(dashboard({
      broken_files: [{
        task_id: "task-broken",
        path: "C:/project/tasks/task-broken.yaml",
        filename: "task-broken.yaml",
        reason: "schema: Input should be 2",
      }],
    }));

    const warning = screen.getByRole("region", { name: "Unreadable task files" });
    expect(warning).toHaveTextContent("1 task file could not be loaded");
    expect(warning).toHaveTextContent("task-broken.yaml — schema: Input should be 2");
  });
});
