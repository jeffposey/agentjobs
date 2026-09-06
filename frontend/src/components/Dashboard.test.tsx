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

  it("no longer carries the count strip's backlog link, and does not leave a dead one", () => {
    // The five-tile strip and the `+N in backlog` link under it came off the Dashboard
    // in task-294. What replaces the link is the Tasks surface's own Status filter,
    // which has a Draft option -- one click on from the "View all" link that stayed.
    renderDashboard(cases[0]!.response);

    expect(screen.queryByRole("link", { name: /in backlog/ })).not.toBeInTheDocument();
    for (const link of screen.getAllByRole("link")) {
      expect(link.getAttribute("href")).not.toBe("/p/inbox/tasks?status=draft");
    }
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

  it("does not render the count-tile strip at all (task-294)", () => {
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

    // Five tiles of counts, and the single largest thing between this page and one
    // screen. They go to task-212's analytics page; until it exists they are simply
    // not here, and none of the five numbers is quoted anywhere else on the Dashboard.
    expect(screen.queryByRole("region", { name: "Task statistics" })).not.toBeInTheDocument();
    expect(screen.queryAllByRole("definition")).toHaveLength(0);
    for (const label of ["Needs you", "In Progress", "Blocked", "Completed", "Total"]) {
      expect(screen.queryByText(label)).not.toBeInTheDocument();
    }
  });

  it("lists a sample of the active tasks and says what it is a sample of", () => {
    // `active_tasks` is uncapped by the server. Rendering it whole is what made this
    // page five thousand pixels tall; the heading carries the real count and the link
    // beside it goes to the surface that holds them all.
    renderDashboard(dashboard({
      active_tasks: Array.from({ length: 9 }, (_, index) => task(`task-active-${index}`)),
    }));

    const heading = screen.getByRole("heading", { name: /Active tasks/ });
    expect(heading).toHaveTextContent("Active tasks (9)");
    expect(screen.getByRole("link", { name: "View all 9 →" })).toHaveAttribute(
      "href",
      "/p/inbox/tasks",
    );
    for (const index of [0, 1, 2]) {
      expect(screen.getByText(`Title of task-active-${index}`)).toBeVisible();
    }
    expect(screen.queryByText("Title of task-active-3")).not.toBeInTheDocument();
  });

  it("splits the page into a pinned glance and a tail that takes what is left", () => {
    // The two regions are the whole of task-294's layout decision: the board and the
    // one call to action are sized to their content, and the lists below them are a
    // remainder with their own scroll. Both are asserted here only for their presence
    // and their contents -- jsdom does not lay out, so the geometry that matters is
    // measured in `frontend/e2e/dashboard-one-screen.spec.ts` instead.
    renderDashboard(dashboard({
      active_tasks: [claimable],
      recent_updates: [
        {
          task_id: "task-next",
          task_title: "Title of task-next",
          timestamp: "2026-08-13T09:00:00Z",
          summary: "Claimed by claude.",
          author: "claude",
        },
      ],
    }));

    const glance = screen.getByTestId("dashboard-glance");
    const tail = screen.getByTestId("dashboard-tail");
    expect(within(glance).getByTestId("next-action")).toBeVisible();
    expect(within(tail).getByRole("heading", { name: /Active tasks/ })).toBeVisible();
    expect(within(tail).getByRole("heading", { name: "Recent updates" })).toBeVisible();
    expect(within(tail).getByText("Claimed by claude.")).toBeVisible();
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

});

// ---------------------------------------------------------------------------
// task-092 -- where the slot board sits, and what an alarm does to it
// ---------------------------------------------------------------------------

describe("Dashboard placement of the slot board", () => {
  function renderWithBoard(value: DashboardResponse) {
    return render(
      <MemoryRouter>
        <Dashboard
          dashboard={value}
          projectId="inbox"
          renderSlotBoard={(statusOnly) => (
            <section data-testid="board" data-status-only={String(statusOnly)}>
              Board
            </section>
          )}
        />
      </MemoryRouter>,
    );
  }

  it("puts the board above the calm rung and asks it for its free cells", () => {
    const { container } = renderWithBoard(
      dashboard({ next_action: "next_up", queue_preview: [claimable] }),
    );

    const board = screen.getByTestId("board");
    expect(board).toHaveAttribute("data-status-only", "false");
    // `next_up` no longer draws a rung of its own: the board is that rung.
    expect(screen.queryByTestId("next-action")).not.toBeInTheDocument();
    expect(container.querySelector("[data-testid='board']")).toBe(board);
  });

  it("keeps an alarm above the board and withholds the board's nudge", () => {
    // task-081's rule, restated for a grid: an alarm must not have to compete with a
    // call to action, and a free cell with a Dispatch button in it is one.
    renderWithBoard(dashboard({ next_action: "blocked", waiting_tasks: [blocked] }));

    const alarm = screen.getByTestId("next-action");
    const board = screen.getByTestId("board");
    expect(alarm).toHaveTextContent("1 Task Blocked on You");
    expect(board).toHaveAttribute("data-status-only", "true");
    expect(alarm.compareDocumentPosition(board) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("treats a broken queue as an alarm too", () => {
    renderWithBoard(dashboard({ next_action: "queue_broken", queue_broken: DUPLICATE }));

    expect(screen.getByTestId("board")).toHaveAttribute("data-status-only", "true");
  });

  it("draws no board at all on a project with no tasks", () => {
    // Six empty cells above "Getting Started" would be the loudest thing on the page
    // and would be saying nothing.
    renderWithBoard(dashboard({ next_action: "empty_project" }));

    expect(screen.queryByTestId("board")).not.toBeInTheDocument();
  });

  it("still shows the board beside the calm rungs that survived", () => {
    renderWithBoard(dashboard({ next_action: "backlog", backlog_tasks: [backlog] }));

    expect(screen.getByTestId("board")).toHaveAttribute("data-status-only", "false");
    expect(screen.getByTestId("next-action")).toHaveTextContent("Backlog awaiting your input");
  });
});
