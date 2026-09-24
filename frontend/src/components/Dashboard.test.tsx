import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import type { DashboardResponse, TaskCardRead } from "../api/types";
import { Dashboard } from "./Dashboard";

function task(id: string, overrides: Partial<TaskCardRead> = {}): TaskCardRead {
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
    status_category: "ready",
    priority: "medium",
    category: "general",
    summary: `Summary of ${id}`,
    can_brief: true,
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
    // which has a Draft option, one click away in the primary nav.
    renderDashboard(cases[0]!.response);

    expect(screen.queryByRole("link", { name: /in backlog/ })).not.toBeInTheDocument();
    for (const link of screen.getAllByRole("link")) {
      expect(link.getAttribute("href")).not.toBe("/p/inbox/tasks?status=draft");
    }
    expect(within(screen.getByTestId("next-action")).queryByText(/Backlog awaiting your input/)).not.toBeInTheDocument();
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

describe("a stalled task in the blocked panel (task-499)", () => {
  /**
   * A claimed task nobody is working sits in this panel beside tasks at the merge gate,
   * because only the owner can re-dispatch it. Its own record says `agent`/`work`
   * throughout -- that is the whole reason nothing noticed for twenty-two hours on
   * 2026-09-19 -- so the row has to say why it is here, or it reads as work in flight
   * that somebody filed in the wrong panel.
   */
  const stalled = task("task-421", {
    lifecycle: "active",
    ball: "agent",
    ball_reason: "revise",
    ball_prompt: "Address the review feedback.",
    display_status: "In progress (claude)",
  });
  const panel = dashboard({
    next_action: "blocked",
    waiting_tasks: [stalled],
    stalled: [
      {
        task_id: "task-421",
        reason: "no_agent",
        quiet_since: "2026-09-19T21:05:00Z",
        quiet_seconds: 22 * 3600,
        threshold_seconds: 3600,
        run_id: "",
      },
    ],
  });

  it("says nobody is on it rather than that it is in progress", () => {
    renderDashboard(panel);

    const row = screen.getByTestId("stalled-task-421");
    expect(within(row).getByText("No agent for 22h 0m")).toBeInTheDocument();
    expect(within(row).queryByText("In progress (claude)")).not.toBeInTheDocument();
  });

  it("replaces the ball prompt, which is addressed to an agent that is gone", () => {
    renderDashboard(panel);

    const row = screen.getByTestId("stalled-task-421");
    expect(within(row).queryByText(/Address the review feedback/)).not.toBeInTheDocument();
    expect(within(row).getByText(/Re-dispatch it, take it over/)).toBeInTheDocument();
  });

  it("leaves an ordinary human-held row alone", () => {
    renderDashboard(dashboard({ next_action: "blocked", waiting_tasks: [blocked] }));

    expect(screen.queryByTestId("stalled-task-blocked")).not.toBeInTheDocument();
    expect(screen.getByText("Waiting for review")).toBeInTheDocument();
  });
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

  it("carries no analytics link: that entry point is the primary nav (task-465)", () => {
    // Task-374 put `Analytics →` on this row and the owner could not find the page.
    // The nav is asserted in PrimaryNav.test.tsx; this guards against the link coming
    // back here and the page having two entry points that disagree.
    renderDashboard(dashboard({}));

    expect(screen.queryByRole("link", { name: "Analytics →" })).not.toBeInTheDocument();
  });

  it("splits the page into a pinned glance and a tail that takes what is left", () => {
    // The two regions are the whole of task-294's layout decision: the board and the
    // one call to action are sized to their content, and the lists below them are a
    // remainder with their own scroll. Both are asserted here only for their presence
    // and their contents -- jsdom does not lay out, so the geometry that matters is
    // measured in `frontend/e2e/dashboard-one-screen.spec.ts` instead.
    renderDashboard(dashboard({
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
    expect(within(tail).getByRole("heading", { name: "Recent updates" })).toBeVisible();
    // Task-557 took the "Active tasks" preview off the page: it duplicated the Tasks
    // surface under a label that collides with the `active` lifecycle.
    expect(screen.queryByRole("heading", { name: /Active tasks/ })).not.toBeInTheDocument();
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

// ---------------------------------------------------------------------------
// task-460 -- where "what just landed" sits
// ---------------------------------------------------------------------------

describe("Dashboard placement of the recently-finished region", () => {
  function renderWithRegion(value: DashboardResponse) {
    return render(
      <MemoryRouter>
        <Dashboard
          dashboard={value}
          projectId="inbox"
          renderSlotBoard={() => <section data-testid="board">Board</section>}
          renderRecentlyFinished={() => <section data-testid="finished">Finished</section>}
        />
      </MemoryRouter>,
    );
  }

  it("puts it in the tail, never in the glance", () => {
    // Nobody acts on a task that is already closed, so it must not take space from the
    // board or stand beside the page's one call to action (task-294, task-081).
    renderWithRegion(dashboard({ next_action: "next_up", queue_preview: [claimable] }));

    const region = screen.getByTestId("finished");
    expect(screen.getByTestId("dashboard-tail")).toContainElement(region);
    expect(screen.getByTestId("dashboard-glance")).not.toContainElement(region);
  });

  it("keeps it at the head of the tail, above the log feed", () => {
    renderWithRegion(dashboard({ next_action: "nothing_claimable" }));

    const tail = screen.getByTestId("dashboard-tail");
    const order = Array.from(tail.children).map((child) =>
      child.getAttribute("data-testid") ?? child.textContent?.slice(0, 13),
    );
    expect(order).toEqual(["finished", "Recent update"]);
  });

  it("still renders the tail when the page supplies no region", () => {
    // Every other test in this file builds a Dashboard without one; the prop is
    // optional and the two sections that predate it must not depend on it.
    render(
      <MemoryRouter>
        <Dashboard dashboard={dashboard({ next_action: "nothing_claimable" })} projectId="inbox" />
      </MemoryRouter>,
    );

    expect(screen.getByText("Recent updates")).toBeInTheDocument();
    expect(screen.queryByTestId("finished")).not.toBeInTheDocument();
  });
});
