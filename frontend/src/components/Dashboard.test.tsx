import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import type { DashboardResponse, TaskRead } from "../api/types";
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
    queue_preview: [],
    next_action: "nothing_claimable",
    broken_files: [],
    identity: { ok: true, user: "jeff", problem: null, detail: "Acting as jeff." },
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

// ---------------------------------------------------------------------------
// task-207 -- a corrupt queue is said out loud rather than answered around
// ---------------------------------------------------------------------------

const DUPLICATE = {
  problems: [
    {
      kind: "duplicate",
      band: "high",
      tasks: ["task-a", "task-b"],
      position: 100,
      message: "band 'high' position 100 is claimed by task-a, task-b",
    },
  ],
  repair_command: "agentjobs queue repair",
};

describe("Dashboard on a broken queue", () => {
  it("names the offending tasks and the repair command in a banner", () => {
    renderDashboard(dashboard({ next_action: "queue_broken", queue_broken: DUPLICATE }));

    const banner = screen.getByRole("alert", { name: "Queue is broken" });
    expect(banner).toHaveTextContent("band 'high' position 100 is claimed by task-a, task-b");
    expect(banner).toHaveTextContent("agentjobs queue repair");
  });

  it("refuses to name a next task rather than reporting an empty backlog", () => {
    renderDashboard(dashboard({ next_action: "queue_broken", queue_broken: DUPLICATE }));

    // "Nothing claimable" would be the worst available lie here: it is the one state
    // in which a person does nothing and feels correct doing it.
    expect(screen.getByTestId("next-action")).toHaveTextContent(
      "The queue cannot say what is next",
    );
    expect(screen.queryByText("Nothing claimable right now")).not.toBeInTheDocument();
  });

  it("keeps the banner up while a more urgent panel holds the ladder", () => {
    // Corruption falsifies "this one is next". It does not falsify "work has stopped
    // on these until you act", which is read off the ball and is still true.
    renderDashboard(
      dashboard({ next_action: "blocked", waiting_tasks: [blocked], queue_broken: DUPLICATE }),
    );

    expect(screen.getByTestId("next-action")).toHaveTextContent("1 Task Blocked on You");
    expect(screen.getByRole("alert", { name: "Queue is broken" })).toBeVisible();
  });

  it("shows no banner at all when the queue is a queue", () => {
    renderDashboard(dashboard({ next_action: "next_up", next_task: claimable }));

    expect(screen.queryByRole("alert", { name: "Queue is broken" })).not.toBeInTheDocument();
  });

  it("offers the whole head of the queue, each with the action the page supplies", () => {
    // The defect task-337 fixed was a dashboard that answered "what do I do next" with
    // a table of drafts. The answer is now the claimable frontier, and each row carries
    // whatever the page hands it -- which on the real page is a Dispatch button.
    const preview = [task("task-one"), task("task-two"), task("task-three")];
    render(
      <MemoryRouter>
        <Dashboard
          dashboard={dashboard({
            next_action: "next_up",
            next_task: preview[0]!,
            queue_preview: preview,
          })}
          projectId="inbox"
          renderQueueAction={(queued) => <button type="button">Dispatch {queued.id}</button>}
        />
      </MemoryRouter>,
    );

    const rows = screen.getAllByTestId("queue-preview-task");
    expect(rows.map((row) => row.dataset.taskId)).toEqual([
      "task-one",
      "task-two",
      "task-three",
    ]);
    for (const queued of preview) {
      expect(screen.getByRole("button", { name: `Dispatch ${queued.id}` })).toBeVisible();
    }
  });

  it("still names one task when the server predates the queue preview", () => {
    // A browser cached from before task-337 talking to a server after it, or the
    // reverse. Either way the panel names the manager's answer rather than nothing.
    renderDashboard(dashboard({ next_action: "next_up", next_task: claimable }));

    expect(screen.getAllByTestId("queue-preview-task")).toHaveLength(1);
    expect(screen.getByRole("heading", { name: "Title of task-next" })).toBeVisible();
  });

  it.each([
    [true, "Nothing is running on this machine. Starting one of these is the useful move."],
    [false, "An agent is already working. These are next in line."],
  ])("says what the machine is doing when idle is %s", (machineIdle, sentence) => {
    render(
      <MemoryRouter>
        <Dashboard
          dashboard={dashboard({ next_action: "next_up", queue_preview: [claimable] })}
          projectId="inbox"
          machineIdle={machineIdle}
        />
      </MemoryRouter>,
    );

    expect(screen.getByText(sentence)).toBeVisible();
  });

  it("says nothing about the machine until it knows", () => {
    // A line that flips from "nothing is running" to "something is" one poll after the
    // page paints is worse than a line that arrives a moment late.
    renderDashboard(dashboard({ next_action: "next_up", queue_preview: [claimable] }));

    expect(screen.queryByText(/Nothing is running/)).not.toBeInTheDocument();
    expect(screen.queryByText(/already working/)).not.toBeInTheDocument();
  });

  it("samples the backlog rather than printing all of it", () => {
    // Six drafts stands in for the 31 that were on screen when this was reported.
    const drafts = Array.from({ length: 6 }, (_, index) =>
      task(`task-draft-${index}`, {
        lifecycle: "draft",
        ball: "human",
        ball_reason: "spec",
        display_status: "Draft",
      }),
    );
    renderDashboard(dashboard({ next_action: "backlog", backlog_tasks: drafts }));

    const action = screen.getByTestId("next-action");
    expect(within(action).getAllByRole("link", { name: /^task-draft-/ })).toHaveLength(5);
    expect(within(action).getByRole("link", { name: "View all 6 drafts →" })).toHaveAttribute(
      "href",
      "/p/inbox/tasks?status=draft",
    );
  });

  it("puts the why-this-one disclosure beside the task it is offering", () => {
    render(
      <MemoryRouter>
        <Dashboard
          dashboard={dashboard({ next_action: "next_up", next_task: claimable })}
          projectId="inbox"
          renderWhyThisOne={() => <p>Because it is first in the high band.</p>}
        />
      </MemoryRouter>,
    );

    expect(screen.getByText("Because it is first in the high band.")).toBeVisible();
  });
});
