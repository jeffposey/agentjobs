import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { Priority, QueueProblemRead, TaskRead } from "../api/types";
import { PRIORITY_COLOURS } from "./PriorityMark";
import { TaskList, type ReorderHandlers } from "./TaskList";
import type { QueueMove } from "./queueOrder";

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
    status_category: "ready",
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

function renderList(
  tasks: Array<TaskRead>,
  entry = "/p/inbox/tasks",
  waitingOnYou: ReadonlySet<string> = new Set(),
) {
  render(
    <MemoryRouter initialEntries={[entry]}>
      <TaskList tasks={tasks} projectId="inbox" waitingOnYou={waitingOnYou} />
      <LocationProbe />
    </MemoryRouter>,
  );
}

/** The closed filter button, whose accessible name changes with what is set. */
function filterButton() {
  return screen.getByRole("button", { name: /^Filters/ });
}

/**
 * Open the popover the way a reader does, and hand back the controls inside it.
 *
 * Every test that touches a select goes through here rather than reaching for a
 * `combobox` directly, which is the point of task-356: those controls do not exist
 * until this gesture has happened.
 */
function openFilters() {
  fireEvent.click(filterButton());
  return screen.getByRole("dialog", { name: "Filters" });
}

describe("TaskList filtering", () => {
  it("defaults to Open and excludes closed tasks", () => {
    renderList([
      task("task-open"),
      task("task-closed", { lifecycle: "closed", ball: null, ball_reason: null, outcome: "completed", display_status: "Completed", status_category: "closed" }),
    ]);

    expect(within(openFilters()).getByRole("combobox", { name: "Status" })).toHaveValue("open");
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
    const filters = openFilters();
    fireEvent.change(within(filters).getByRole("combobox", { name: "Status" }), { target: { value: "all" } });
    fireEvent.change(within(filters).getByRole("combobox", { name: "Priority" }), { target: { value: "high" } });
    fireEvent.change(within(filters).getByRole("combobox", { name: "Scope" }), { target: { value: "test" } });

    const url = screen.getByTestId("location").textContent ?? "";
    expect(url).toContain("q=high");
    expect(url).toContain("status=all");
    expect(url).toContain("priority=high");
    expect(url).toContain("scope=test");
  });

  it.each(["task-058", "058", "TASK-058"])("finds a task by its id typed as %s", (query) => {
    renderList([
      task("task-058-multi-project-gui", { title: "Nothing in this title is numeric" }),
      task("task-101-unrelated"),
    ], "/p/inbox/tasks?status=all");

    fireEvent.change(screen.getByRole("searchbox", { name: "Search tasks" }), { target: { value: query } });

    const table = screen.getByRole("region", { name: "Tasks" });
    expect(within(table).getByText("task-058-multi-project-gui")).toBeVisible();
    expect(within(table).queryByText("task-101-unrelated")).not.toBeInTheDocument();
  });

  // task-456: both parks are `external`, and one of them needs nobody. Before this they
  // shared one filter option, so a reader could separate neither.
  describe("separating a self-clearing wait from a real blocker", () => {
    const quota = () =>
      task("task-on-a-quota", {
        lifecycle: "active",
        ball: "external",
        ball_reason: "service",
        ball_prompt: "The limit resets at 2026-09-18T21:30:00Z. Nothing to do.",
        display_status: "Quota reset",
        status_category: "not_now",
        assignment: { owner: "claude", eligible: [] },
        self_clearing_wait: { kind: "usage_limit", resets_at: "2026-09-18T21:30:00Z" },
      });
    const vendor = () =>
      task("task-on-a-vendor", {
        lifecycle: "active",
        ball: "external",
        ball_reason: "service",
        ball_prompt: "Their API has been 503 since this morning.",
        display_status: "Blocked",
        status_category: "not_now",
        assignment: { owner: "claude", eligible: [] },
        self_clearing_wait: null,
      });

    it("shows only the real blocker under Blocked", () => {
      renderList([quota(), vendor()], "/p/inbox/tasks?status=external");

      const table = screen.getByRole("region", { name: "Tasks" });
      expect(within(table).getByText("task-on-a-vendor")).toBeVisible();
      expect(within(table).queryByText("task-on-a-quota")).not.toBeInTheDocument();
    });

    it("shows only the self-clearing wait under Waiting on a reset", () => {
      renderList([quota(), vendor()], "/p/inbox/tasks?status=reset");

      const table = screen.getByRole("region", { name: "Tasks" });
      expect(within(table).getByText("task-on-a-quota")).toBeVisible();
      expect(within(table).queryByText("task-on-a-vendor")).not.toBeInTheDocument();
    });

    it("offers the option to a reader who opens the filters", () => {
      renderList([quota(), vendor()], "/p/inbox/tasks?status=all");
      const popover = openFilters();

      fireEvent.change(within(popover).getByLabelText("Status"), { target: { value: "reset" } });

      expect(screen.getByTestId("location")).toHaveTextContent("status=reset");
      const table = screen.getByRole("region", { name: "Tasks" });
      expect(within(table).getByText("Quota reset")).toBeVisible();
      expect(within(table).queryByText("task-on-a-vendor")).not.toBeInTheDocument();
    });

    it("leaves both in the list when no status filter is set", () => {
      renderList([quota(), vendor()], "/p/inbox/tasks?status=all");

      const table = screen.getByRole("region", { name: "Tasks" });
      expect(within(table).getByText("task-on-a-quota")).toBeVisible();
      expect(within(table).getByText("task-on-a-vendor")).toBeVisible();
    });
  });

  // task-509: a task whose branch is being merged is `active`/`agent`/`work`, exactly
  // like a task an agent is editing, so the `active` option cannot separate them.
  describe("separating a task being merged from one an agent is working", () => {
    const finishing = () =>
      task("task-being-merged", {
        lifecycle: "active",
        ball: "agent",
        ball_reason: "work",
        display_status: "Finishing",
        assignment: { owner: "claude", eligible: [] },
        live_finish: {
          finish_id: "fin_a1b2c3d4",
          state: "running",
          started_at: "2026-09-20T15:26:00Z",
          current_step: "gate",
          step_meaning: "Running the full gate on the rebased branch",
          branch: "feat/task-509-finishing-status",
        },
      });
    const worked = () =>
      task("task-being-worked", {
        lifecycle: "active",
        ball: "agent",
        ball_reason: "work",
        display_status: "In progress (claude)",
        assignment: { owner: "claude", eligible: [] },
        live_finish: null,
      });

    it("shows only the merging one under Finishing", () => {
      renderList([finishing(), worked()], "/p/inbox/tasks?status=finishing");

      const table = screen.getByRole("region", { name: "Tasks" });
      expect(within(table).getByText("task-being-merged")).toBeVisible();
      expect(within(table).queryByText("task-being-worked")).not.toBeInTheDocument();
    });

    it("still shows both under Active, which is what they both are", () => {
      renderList([finishing(), worked()], "/p/inbox/tasks?status=active");

      const table = screen.getByRole("region", { name: "Tasks" });
      expect(within(table).getByText("task-being-merged")).toBeVisible();
      expect(within(table).getByText("task-being-worked")).toBeVisible();
    });

    it("offers the option to a reader who opens the filters", () => {
      renderList([finishing(), worked()], "/p/inbox/tasks?status=all");
      const popover = openFilters();

      fireEvent.change(within(popover).getByLabelText("Status"), { target: { value: "finishing" } });

      expect(screen.getByTestId("location")).toHaveTextContent("status=finishing");
      const table = screen.getByRole("region", { name: "Tasks" });
      // The rendered label, not the presence of a row: the chip is the whole point.
      expect(within(table).getByText("Finishing")).toBeVisible();
      expect(within(table).queryByText("task-being-worked")).not.toBeInTheDocument();
    });
  });

  describe("Waiting on you (task-499)", () => {
    /**
     * The one filter this list cannot compute from its own rows. Half the waiting set is
     * derived from the machine's run ledger: a claimed task nobody is working reads
     * `active`/`agent`/`work` on every field it has, which is precisely why nothing
     * noticed one for twenty-two hours on 2026-09-19. So the set arrives as the
     * attention episode's membership -- the same answer the header badge draws -- and a
     * notification saying "2 tasks are waiting on you" and the list it links to are the
     * same two by construction.
     */
    const review = () =>
      task("task-at-the-gate", {
        lifecycle: "active",
        ball: "human",
        ball_reason: "review",
        ball_prompt: "Approve or request changes.",
        display_status: "Waiting for review",
        assignment: { owner: "claude", eligible: [] },
      });
    const stalled = () =>
      task("task-421", {
        lifecycle: "active",
        ball: "agent",
        ball_reason: "revise",
        ball_prompt: "Address the review feedback.",
        display_status: "In progress (claude)",
        assignment: { owner: "claude", eligible: [] },
      });

    it("holds a stalled task that no ball on the record would select", () => {
      renderList(
        [review(), stalled(), task("task-idle")],
        "/p/inbox/tasks?status=attention",
        new Set(["task-at-the-gate", "task-421"]),
      );

      const table = screen.getByRole("region", { name: "Tasks" });
      expect(within(table).getByText("task-421")).toBeVisible();
      expect(within(table).getByText("task-at-the-gate")).toBeVisible();
      expect(within(table).queryByText("task-idle")).not.toBeInTheDocument();
    });

    it("selects nothing when nothing is waiting, rather than falling back to a ball", () => {
      renderList([review(), stalled()], "/p/inbox/tasks?status=attention");

      const table = screen.getByRole("region", { name: "Tasks" });
      expect(within(table).queryByText("task-at-the-gate")).not.toBeInTheDocument();
      expect(within(table).queryByText("task-421")).not.toBeInTheDocument();
    });

    it("offers the option to a reader who opens the filters", () => {
      renderList([review(), stalled()], "/p/inbox/tasks?status=all", new Set(["task-421"]));
      const popover = openFilters();

      fireEvent.change(within(popover).getByLabelText("Status"), {
        target: { value: "attention" },
      });

      expect(screen.getByTestId("location")).toHaveTextContent("status=attention");
      const table = screen.getByRole("region", { name: "Tasks" });
      expect(within(table).getByText("task-421")).toBeVisible();
      expect(within(table).queryByText("task-at-the-gate")).not.toBeInTheDocument();
    });
  });

  it("keeps a superseded task distinguishable from a completed one on the list", () => {
    renderList([
      task("task-058-superseded", { lifecycle: "closed", ball: null, ball_reason: null, outcome: "superseded", display_status: "Superseded", status_category: "closed_unfinished" }),
      task("task-059-completed", { lifecycle: "closed", ball: null, ball_reason: null, outcome: "completed", display_status: "Completed" }),
    ], "/p/inbox/tasks?status=closed");

    const table = screen.getByRole("region", { name: "Tasks" });
    expect(within(table).getByText("Superseded")).toBeVisible();
    expect(within(table).getByText("Completed")).toBeVisible();
    expect(within(table).queryByText("Done")).not.toBeInTheDocument();
  });

  it("explains dependency blocks in words on the task row", () => {
    renderList([task("task-blocked", {
      actionable: false,
      unmet_needs: ["task-prerequisite (still open)"],
      display_status: "Blocked",
      status_category: "not_now",
    })]);

    const rows = screen.getByRole("region", { name: "Tasks" });
    expect(within(rows).getByText("Blocked")).toBeVisible();
    expect(within(rows).getByText("Waiting for task-prerequisite (still open)")).toBeVisible();
  });

  it("says what claiming an umbrella would get you", () => {
    renderList([task("task-umbrella", {
      actionable: false,
      open_children_count: 2,
      display_status: "Sub-tasks",
      status_category: "not_now",
    })]);

    expect(screen.getByText("Sub-tasks")).toBeVisible();
    // task-164: an epic is claimable now, and the claim is for supervision. The old
    // copy said the children "must finish first", which the claim no longer requires.
    expect(screen.getByText(/2 open sub-tasks to finish\./)).toBeVisible();
    expect(screen.getByText(/Claim it to supervise them/)).toBeVisible();
  });
});

describe("TaskList filter popover", () => {
  it("renders no filter control at all until the button is pressed", () => {
    renderList([task("task-open")]);

    // Not merely hidden -- absent. The whole point of the change is the vertical space,
    // and an `sr-only` or `hidden` panel would still be four controls in the document
    // for a screen reader to walk past.
    expect(screen.queryByRole("combobox", { name: "Status" })).not.toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: "Priority" })).not.toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: "Scope" })).not.toBeInTheDocument();
    expect(screen.queryByRole("dialog", { name: "Filters" })).not.toBeInTheDocument();
    expect(filterButton()).toHaveAttribute("aria-expanded", "false");

    openFilters();

    expect(screen.getByRole("combobox", { name: "Status" })).toBeVisible();
    expect(filterButton()).toHaveAttribute("aria-expanded", "true");
  });

  it("says on the closed button that a filter is set, in the badge and in its name", () => {
    // The defect this guards against is the one the spec calls worse than the one being
    // fixed: a list hiding two thirds of the backlog looking like an empty backlog.
    renderList([task("task-high", { priority: "high" })]);
    expect(screen.queryByTestId("active-filter-count")).not.toBeInTheDocument();
    expect(filterButton()).toHaveAccessibleName("Filters, none set");

    fireEvent.change(within(openFilters()).getByRole("combobox", { name: "Priority" }), {
      target: { value: "high" },
    });
    fireEvent.keyDown(document, { key: "Escape" });

    expect(screen.getByTestId("active-filter-count")).toHaveTextContent("1");
    expect(filterButton()).toHaveAccessibleName("Filters, 1 set: Priority high");
  });

  it("counts only the filters the popover holds, never the visible search box", () => {
    renderList([task("task-open")]);

    fireEvent.change(screen.getByRole("searchbox", { name: "Search tasks" }), {
      target: { value: "queue" },
    });

    // The search box is on screen with its own text in it. Lighting the badge for a
    // filter the reader can already read is the noise that stops an indicator working.
    expect(screen.queryByTestId("active-filter-count")).not.toBeInTheDocument();
    expect(filterButton()).toHaveAccessibleName("Filters, none set");
  });

  it("arrives filtered from a pasted URL, with the button saying so before it is opened", () => {
    renderList(
      [task("task-high", { priority: "high", tags: ["test"] }), task("task-plain")],
      "/p/inbox/tasks?status=all&priority=high&scope=test",
    );

    expect(screen.getByTestId("active-filter-count")).toHaveTextContent("3");
    expect(filterButton()).toHaveAccessibleName(
      "Filters, 3 set: Status all, Priority high, Scope test",
    );
    expect(screen.getByText("task-high")).toBeVisible();
    expect(screen.queryByText("task-plain")).not.toBeInTheDocument();

    const filters = openFilters();
    expect(within(filters).getByRole("combobox", { name: "Status" })).toHaveValue("all");
    expect(within(filters).getByRole("combobox", { name: "Priority" })).toHaveValue("high");
    expect(within(filters).getByRole("combobox", { name: "Scope" })).toHaveValue("test");
  });

  it("ignores a URL value outside the allowed set rather than counting it as a filter", () => {
    // `filterValue()` falls back for an unknown value, so the list is not filtered. A
    // badge counting it would be reporting a filter that is not being applied.
    renderList([task("task-open")], "/p/inbox/tasks?priority=nonsense");

    expect(screen.queryByTestId("active-filter-count")).not.toBeInTheDocument();
  });

  it("clears every filter, the search box included, in one gesture", () => {
    renderList(
      [task("task-high", { priority: "high", tags: ["test"] })],
      "/p/inbox/tasks?q=high&status=all&priority=high&scope=test",
    );

    fireEvent.click(within(openFilters()).getByRole("button", { name: "Clear all filters" }));

    expect(screen.getByTestId("location")).toHaveTextContent("/p/inbox/tasks");
    expect(screen.getByTestId("location").textContent).not.toContain("?");
    expect(screen.getByRole("searchbox", { name: "Search tasks" })).toHaveValue("");
    expect(screen.queryByTestId("active-filter-count")).not.toBeInTheDocument();
  });

  it("offers nothing to clear when nothing is set", () => {
    renderList([task("task-open")]);

    expect(within(openFilters()).getByRole("button", { name: "Clear all filters" })).toBeDisabled();
  });

  it("moves focus into the popover on open and back to the button on Escape", () => {
    renderList([task("task-open")]);

    openFilters();
    expect(document.activeElement).toBe(screen.getByRole("combobox", { name: "Status" }));

    fireEvent.keyDown(document, { key: "Escape" });

    expect(screen.queryByRole("dialog", { name: "Filters" })).not.toBeInTheDocument();
    expect(document.activeElement).toBe(filterButton());
    expect(filterButton()).toHaveAttribute("aria-expanded", "false");
  });

  it("closes on a click outside it and leaves the click its own target", () => {
    renderList([task("task-open")]);
    openFilters();

    // `mousedown` and not `click`, because that is what the popover listens for: a
    // reader clicking a task row must not have the row swallowed by a dismissal.
    fireEvent.mouseDown(screen.getByRole("region", { name: "Tasks" }));

    expect(screen.queryByRole("dialog", { name: "Filters" })).not.toBeInTheDocument();
    // Focus is deliberately *not* pulled back here: the click has a target of its own.
    expect(document.activeElement).not.toBe(filterButton());
  });

  it("stays open for a click on its own controls", () => {
    renderList([task("task-open")]);
    const filters = openFilters();

    fireEvent.mouseDown(within(filters).getByRole("combobox", { name: "Priority" }));

    expect(screen.getByRole("dialog", { name: "Filters" })).toBeVisible();
  });

  it("is a real button, which is what makes Enter and Space open it", () => {
    // jsdom does not implement a button's own keyboard activation, so asserting a
    // keypress here would be asserting a fact about jsdom. What can be checked is the
    // property the browser's behaviour rests on; `e2e/filter-popover.spec.ts` presses
    // the keys for real in Chromium.
    renderList([task("task-open")]);

    const button = filterButton();
    expect(button.tagName).toBe("BUTTON");
    expect(button).toHaveAttribute("type", "button");
    expect(button).toHaveAttribute("aria-haspopup", "dialog");
  });

  it("closes again when the button is pressed a second time", () => {
    renderList([task("task-open")]);
    openFilters();

    fireEvent.click(filterButton());

    expect(screen.queryByRole("dialog", { name: "Filters" })).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// task-207 -- the list shows the real order, and you can change it without a mouse
// ---------------------------------------------------------------------------

function queued(id: string, position: number, priority: Priority = "high"): TaskRead {
  return task(id, { priority, queue_position: position });
}

function renderQueue(
  tasks: Array<TaskRead>,
  options: {
    reorder?: ReorderHandlers | null;
    problems?: Array<QueueProblemRead>;
    unavailable?: string | null;
    entry?: string;
  } = {},
) {
  render(
    <MemoryRouter initialEntries={[options.entry ?? "/p/inbox/tasks"]}>
      <TaskList
        tasks={tasks}
        projectId="inbox"
        reorder={options.reorder ?? null}
        queueProblems={options.problems ?? []}
        reorderUnavailable={options.unavailable ?? null}
      />
    </MemoryRouter>,
  );
}

/** The ids in the order the table renders them. */
function renderedOrder() {
  return Array.from(document.querySelectorAll("[data-task]")).map(
    (row) => row.getAttribute("data-task") ?? "",
  );
}

function rowFor(taskId: string) {
  return document.querySelector(`[data-task="${taskId}"]`) as HTMLElement;
}

function queueCell(taskId: string) {
  return rowFor(taskId).querySelector('[data-label="Queue"]') as HTMLElement;
}

function grip(taskId: string) {
  return screen.getByRole("button", { name: new RegExp(`^Reorder ${taskId},`) });
}

/** A move handler that succeeds and has nothing to say -- the ordinary case. */
function silentMove() {
  return vi.fn().mockResolvedValue({ warnings: [], undo: null });
}

function accepting(): ReorderHandlers & { moves: Array<[string, QueueMove]> } {
  const moves: Array<[string, QueueMove]> = [];
  return {
    moves,
    move: async (taskId, move) => {
      moves.push([taskId, move]);
      return { warnings: [], undo: null };
    },
    reprioritize: async () => {},
    keep: async () => {},
  };
}

/**
 * Take over the frame clock and the scroller, so an autoscroll can be asserted rather
 * than waited for. Every spy is undone by `restore`.
 */
function frameHarness() {
  const frames: Array<(time: number) => void> = [];
  let scrolled = 0;
  let now = 0;
  vi.spyOn(window, "scrollBy").mockImplementation(((_x: number, y: number) => {
    scrolled += y;
  }) as typeof window.scrollBy);
  vi.spyOn(window, "requestAnimationFrame").mockImplementation((callback) => frames.push(callback));
  vi.spyOn(window, "cancelAnimationFrame").mockImplementation(() => {});
  return {
    /** Everything currently scheduled, `ms` later. The loop reschedules as it runs. */
    run: (ms = 16) => {
      now += ms;
      for (const callback of frames.splice(0, frames.length)) callback(now);
    },
    get scrolled() {
      return scrolled;
    },
    restore: () => vi.restoreAllMocks(),
  };
}

/** A drag hovering at `clientY`, as the browser reports it to the document. */
function dragOverAt(clientY: number) {
  document.dispatchEvent(new MouseEvent("dragover", { bubbles: true, clientY }));
}

describe("TaskList queue order", () => {
  it("names the handle for what it holds, and announces the keys as keys", () => {
    // The name is what a screen reader reads on every row a person tabs through, so it
    // says what this handle is and where the task stands -- and nothing else. The keys
    // used to be a sentence inside it: twenty-five words per row, describing shortcuts
    // as prose, on a control that is announced as a button and does nothing when a
    // button's activation keys are pressed on it.
    renderQueue([queued("task-a", 100), queued("task-b", 200)], { reorder: accepting() });

    const handle = grip("task-a");
    expect(handle).toHaveAccessibleName("Reorder task-a, high band, position 100");
    expect(handle).toHaveAttribute(
      "aria-keyshortcuts",
      "Alt+ArrowUp Alt+ArrowDown Alt+Home Alt+End",
    );

    // The instructions still exist, once, as the handle's description -- and the
    // reference resolves, which is the half an aria-describedby usually gets wrong.
    const described = handle.getAttribute("aria-describedby") ?? "";
    expect(document.getElementById(described)).toHaveTextContent(
      /step it through its priority band/,
    );

    // Still reachable: a keyboard lands on it, which is the whole reason it is focusable.
    handle.focus();
    expect(handle).toHaveFocus();
  });

  it("renders the server's order rather than newest-first", () => {
    // Handed to the component in queue order, with `updated` deliberately upside down:
    // the deleted sort would have put task-c first, and so would any client-side rule
    // still keyed on a timestamp.
    renderQueue([
      { ...queued("task-a", 100), updated: "2026-08-13T01:00:00Z" },
      { ...queued("task-b", 200), updated: "2026-08-13T02:00:00Z" },
      { ...queued("task-c", 300), updated: "2026-08-13T03:00:00Z" },
    ]);

    expect(renderedOrder()).toEqual(["task-a", "task-b", "task-c"]);
  });

  it("shows each task's place in line as a value, not just a tooltip", () => {
    renderQueue([
      queued("task-a", 100),
      task("task-closed", {
        lifecycle: "closed",
        ball: null,
        ball_reason: null,
        outcome: "completed",
        display_status: "Completed",
      }),
    ], { entry: "/p/inbox/tasks?status=all" });

    expect(rowFor("task-a").getAttribute("data-queue-position")).toBe("100");
    expect(queueCell("task-a")).toHaveTextContent("100");
    // Nothing is claimed about a closed task's place, because it has none.
    expect(queueCell("task-closed")).toHaveTextContent("—");
    expect(rowFor("task-closed").getAttribute("data-queue-position")).toBe("");
  });

  it("fires exactly one move per keypress, stepping through the band", async () => {
    const handlers = accepting();
    renderQueue([queued("task-a", 100), queued("task-b", 200), queued("task-c", 300)], {
      reorder: handlers,
    });

    fireEvent.keyDown(grip("task-b"), { key: "ArrowUp", altKey: true });
    await waitFor(() => expect(handlers.moves).toHaveLength(1));
    expect(handlers.moves[0]).toEqual(["task-b", { before: "task-a" }]);
  });

  it("writes nothing for a gesture that would not move the task", async () => {
    const handlers = accepting();
    renderQueue([queued("task-a", 100), queued("task-b", 200)], { reorder: handlers });

    // task-a is already first and task-b already last. A move that lands a task where
    // it already is still writes a queue_move entry recording a decision nobody made.
    fireEvent.keyDown(grip("task-a"), { key: "Home", altKey: true });
    fireEvent.keyDown(grip("task-a"), { key: "ArrowUp", altKey: true });
    fireEvent.keyDown(grip("task-b"), { key: "End", altKey: true });
    await waitFor(() => expect(renderedOrder()).toEqual(["task-a", "task-b"]));

    expect(handlers.moves).toEqual([]);
  });

  it("ignores an arrow key without Alt, so ordinary navigation still works", () => {
    const handlers = accepting();
    renderQueue([queued("task-a", 100), queued("task-b", 200)], { reorder: handlers });

    fireEvent.keyDown(grip("task-b"), { key: "ArrowUp" });

    expect(handlers.moves).toHaveLength(0);
  });

  it("shows the new order immediately and announces it", async () => {
    renderQueue([queued("task-a", 100), queued("task-b", 200)], { reorder: accepting() });

    fireEvent.keyDown(grip("task-b"), { key: "ArrowUp", altKey: true });

    await waitFor(() => expect(renderedOrder()).toEqual(["task-b", "task-a"]));
    expect(screen.getByText("task-b moved ahead of task-a in the high band.")).toBeInTheDocument();
  });

  it("keeps focus on the task that moved, so a second press moves the same task", async () => {
    // React reorders rows by moving their DOM nodes, and a browser drops focus from a
    // node that is detached and reinserted. Without putting it back, the keyboard path
    // works exactly once -- and the second press moves whichever task slid into the
    // vacated row, which is worse than doing nothing.
    const handlers = accepting();
    renderQueue([queued("task-a", 100), queued("task-b", 200), queued("task-c", 300)], {
      reorder: handlers,
    });

    grip("task-c").focus();
    fireEvent.keyDown(grip("task-c"), { key: "ArrowUp", altKey: true });
    await waitFor(() => expect(renderedOrder()).toEqual(["task-a", "task-c", "task-b"]));
    expect(document.activeElement).toBe(grip("task-c"));

    // Pressed again without touching anything: still task-c that moves.
    fireEvent.keyDown(document.activeElement as HTMLElement, { key: "ArrowUp", altKey: true });
    await waitFor(() => expect(renderedOrder()).toEqual(["task-c", "task-a", "task-b"]));
    expect(handlers.moves.map(([id]) => id)).toEqual(["task-c", "task-c"]);
  });

  it("rolls the optimistic order back and says so when the move is refused", async () => {
    const reorder: ReorderHandlers = {
      move: vi.fn().mockRejectedValue(new Error("409")),
      reprioritize: vi.fn(),
      keep: vi.fn(),
    };
    renderQueue([queued("task-a", 100), queued("task-b", 200)], { reorder });

    fireEvent.keyDown(grip("task-b"), { key: "ArrowUp", altKey: true });

    // Back to what the server last said -- not left showing a place the task is not in.
    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent(
        /task-b could not be moved, so the list has been put back/,
      ),
    );
    expect(renderedOrder()).toEqual(["task-a", "task-b"]);
  });

  it("asks before a drag changes a priority as well as a place", async () => {
    const reorder: ReorderHandlers = { move: silentMove(), reprioritize: vi.fn(), keep: vi.fn() };
    renderQueue([queued("task-a", 100), queued("task-m", 100, "medium")], { reorder });

    fireEvent.dragStart(grip("task-a"));
    fireEvent.drop(rowFor("task-m"));

    const dialog = await screen.findByRole("alertdialog", { name: "Confirm a priority change" });
    expect(dialog).toHaveTextContent(/leave the high band for the medium band/);
    expect(reorder.reprioritize).not.toHaveBeenCalled();

    fireEvent.click(within(dialog).getByRole("button", { name: "Move it to medium" }));
    await waitFor(() =>
      expect(reorder.reprioritize).toHaveBeenCalledWith("task-a", "medium", "task-m"),
    );
    // Still one decision per gesture: a band change is a reprioritize, never a move.
    expect(reorder.move).not.toHaveBeenCalled();
  });

  it("moves within a band on a drop without asking anything", async () => {
    const handlers = accepting();
    renderQueue([queued("task-a", 100), queued("task-b", 200), queued("task-c", 300)], {
      reorder: handlers,
    });

    fireEvent.dragStart(grip("task-a"));
    fireEvent.drop(rowFor("task-c"));

    await waitFor(() => expect(handlers.moves).toHaveLength(1));
    expect(handlers.moves[0]).toEqual(["task-a", { after: "task-c" }]);
  });

  it("scrolls the page while a drag is held at the bottom edge, and stops on the drop", async () => {
    // The defect this covers: a list taller than the window could only be dropped on
    // rows that were already on screen, because nothing scrolled. What is asserted here
    // is the *mechanism* -- that the loop runs and is torn down. Whether a person can
    // actually reach an off-screen row is not a thing jsdom can answer, and the task
    // record carries a by-hand check for it instead.
    const harness = frameHarness();
    try {
      const handlers = accepting();
      renderQueue([queued("task-a", 100), queued("task-c", 300)], { reorder: handlers });

      fireEvent.dragStart(grip("task-a"));
      dragOverAt(window.innerHeight);
      harness.run();
      harness.run();
      expect(harness.scrolled).toBeGreaterThan(0);

      const before = harness.scrolled;
      fireEvent.drop(rowFor("task-c"));
      harness.run();
      harness.run();
      expect(harness.scrolled).toBe(before);
      await waitFor(() => expect(handlers.moves).toHaveLength(1));
    } finally {
      harness.restore();
    }
  });

  it("scrolls the other way at the top edge", () => {
    const harness = frameHarness();
    try {
      renderQueue([queued("task-a", 100), queued("task-b", 200)], { reorder: accepting() });
      fireEvent.dragStart(grip("task-a"));
      dragOverAt(0);
      harness.run();
      harness.run();
      expect(harness.scrolled).toBeLessThan(0);
      fireEvent.dragEnd(grip("task-a"));
    } finally {
      harness.restore();
    }
  });

  it("does not scroll for a drag this list did not start", () => {
    // A link, or a file dragged in from the desktop, crosses this page too. The loop
    // exists only between a grip's dragstart and the end of that gesture.
    const harness = frameHarness();
    try {
      renderQueue([queued("task-a", 100), queued("task-b", 200)], { reorder: accepting() });
      dragOverAt(window.innerHeight);
      harness.run();
      harness.run();
      expect(harness.scrolled).toBe(0);
    } finally {
      harness.restore();
    }
  });

  it("stops scrolling when a drag is cancelled rather than dropped", () => {
    // Escape, or letting go over something that is not a row: `dragend` and nothing
    // else. A loop that survived it would scroll the page under a person who is no
    // longer dragging anything.
    const harness = frameHarness();
    try {
      renderQueue([queued("task-a", 100), queued("task-b", 200)], { reorder: accepting() });
      fireEvent.dragStart(grip("task-a"));
      dragOverAt(window.innerHeight);
      harness.run();
      harness.run();
      expect(harness.scrolled).toBeGreaterThan(0);

      const before = harness.scrolled;
      fireEvent.dragEnd(grip("task-a"));
      dragOverAt(window.innerHeight);
      harness.run();
      harness.run();
      expect(harness.scrolled).toBe(before);
    } finally {
      harness.restore();
    }
  });

  it("carries the dragged id under a private type, so it cannot be dropped into another application", () => {
    renderQueue([queued("task-a", 100), queued("task-b", 200)], { reorder: accepting() });
    const dataTransfer = { effectAllowed: "", setData: vi.fn() };

    fireEvent.dragStart(grip("task-a"), { dataTransfer });

    expect(dataTransfer.effectAllowed).toBe("move");
    expect(dataTransfer.setData).toHaveBeenCalledWith("application/x-agentjobs-task-id", "task-a");
    expect(dataTransfer.setData).not.toHaveBeenCalledWith("text/plain", expect.anything());
  });

  it("drags a picture of the row, not of the handle", () => {
    // Left to itself the browser drags an image of whatever carries `draggable`, which
    // is the 20px grip: the gesture then shows a glyph floating over a list in which
    // nothing appears to be happening.
    renderQueue([queued("task-a", 100), queued("task-b", 200)], { reorder: accepting() });
    const dataTransfer = { effectAllowed: "", setData: vi.fn(), setDragImage: vi.fn() };

    fireEvent.dragStart(grip("task-a"), { dataTransfer });

    expect(dataTransfer.setDragImage).toHaveBeenCalledWith(
      rowFor("task-a"),
      expect.any(Number),
      expect.any(Number),
    );
  });

  it("shows the row that is moving to be in flight, for the length of the gesture", () => {
    renderQueue([queued("task-a", 100), queued("task-b", 200)], { reorder: accepting() });

    fireEvent.dragStart(grip("task-a"));
    expect(rowFor("task-a")).toHaveAttribute("data-dragging", "true");
    expect(rowFor("task-b")).not.toHaveAttribute("data-dragging");

    fireEvent.dragEnd(grip("task-a"));
    expect(rowFor("task-a")).not.toHaveAttribute("data-dragging");
  });

  it("marks the side of the row the drop will actually use", async () => {
    // The assertion that matters is the pairing: the side drawn while the pointer is
    // held there and the side the drop uses come from one function, and this is what
    // would catch them drifting apart.
    const handlers = accepting();
    renderQueue([queued("task-a", 100), queued("task-b", 200), queued("task-c", 300)], {
      reorder: handlers,
    });

    fireEvent.dragStart(grip("task-a"));
    fireEvent.dragOver(rowFor("task-c"));
    expect(rowFor("task-c")).toHaveAttribute("data-drop-side", "after");

    fireEvent.drop(rowFor("task-c"));
    await waitFor(() => expect(handlers.moves).toHaveLength(1));
    expect(handlers.moves[0]).toEqual(["task-a", { after: "task-c" }]);
  });

  it("marks the other side when the row is travelling the other way", async () => {
    const handlers = accepting();
    renderQueue([queued("task-a", 100), queued("task-b", 200), queued("task-c", 300)], {
      reorder: handlers,
    });

    fireEvent.dragStart(grip("task-c"));
    fireEvent.dragOver(rowFor("task-a"));
    expect(rowFor("task-a")).toHaveAttribute("data-drop-side", "before");

    fireEvent.drop(rowFor("task-a"));
    await waitFor(() => expect(handlers.moves).toHaveLength(1));
    expect(handlers.moves[0]).toEqual(["task-c", { before: "task-a" }]);
  });

  it("draws no line on a row that would not take the drop, and none once the drag is over", () => {
    renderQueue([queued("task-a", 100), queued("task-b", 200)], { reorder: accepting() });

    fireEvent.dragStart(grip("task-a"));
    fireEvent.dragOver(rowFor("task-b"));
    expect(rowFor("task-b")).toHaveAttribute("data-drop-side", "after");

    // Back over the row being dragged: there is no move to make here, so there is
    // nothing to promise.
    fireEvent.dragOver(rowFor("task-a"));
    expect(rowFor("task-a")).not.toHaveAttribute("data-drop-side");
    expect(rowFor("task-b")).not.toHaveAttribute("data-drop-side");

    fireEvent.dragOver(rowFor("task-b"));
    fireEvent.dragEnd(grip("task-a"));
    expect(rowFor("task-b")).not.toHaveAttribute("data-drop-side");
  });

  it("gives the tree the same feedback as the table", () => {
    // One `dragProps` serves both shapes, and this is what says so.
    renderTree(epic(), { reorder: accepting() });

    fireEvent.dragStart(grip("task-after"));
    fireEvent.dragOver(rowFor("task-parent"));

    expect(rowFor("task-after")).toHaveAttribute("data-dragging", "true");
    expect(rowFor("task-parent")).toHaveAttribute("data-drop-side", "before");
  });

  it("refuses to offer an order it cannot justify", () => {
    renderQueue([queued("task-a", 100), queued("task-b", 100)], {
      reorder: accepting(),
      problems: [
        {
          kind: "duplicate",
          band: "high",
          tasks: ["task-a", "task-b"],
          position: 100,
          message: "band 'high' position 100 is claimed by task-a, task-b",
        },
      ],
    });

    // The banner naming the problem belongs to the surface now, not to this list --
    // it is asserted in App.shell.test.tsx, where it renders outside both scroll
    // regions. What the list still owes is this: every gesture places a task relative
    // to a neighbour, and corruption is exactly what makes a neighbour's position
    // untrustworthy.
    expect(screen.queryByRole("button", { name: /^Reorder task-a,/ })).not.toBeInTheDocument();
  });

  it("takes reordering away from the broken band only, not from the whole screen", () => {
    // The same scoping selection uses: a duplicate in one band falsifies nothing about
    // another, and disabling every band over it would punish the wrong one.
    renderQueue([queued("task-a", 100), queued("task-b", 100), queued("task-m", 100, "medium")], {
      reorder: accepting(),
      problems: [
        {
          kind: "duplicate",
          band: "high",
          tasks: ["task-a", "task-b"],
          position: 100,
          message: "band 'high' position 100 is claimed by task-a, task-b",
        },
      ],
    });

    expect(screen.queryByRole("button", { name: /^Reorder task-a,/ })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Reorder task-m,/ })).toBeVisible();
  });

  it("says why reordering is off when the project names nobody to attribute it to", () => {
    renderQueue([queued("task-a", 100)], {
      reorder: null,
      unavailable: "Reordering is off because this project configures no human actor.",
    });

    expect(screen.getByText(/configures no human actor/)).toBeVisible();
    expect(screen.queryByRole("button", { name: /^Reorder task-a,/ })).not.toBeInTheDocument();
  });
});

describe("TaskList queue-move notice", () => {
  const blocked = {
    kind: "promoted_unclaimable",
    message: "task-c cannot be claimed where you have just put it: waiting on task-a (still open). The queue will skip past it.",
    tasks: ["task-c"],
  };

  function warning(kind: string, message: string) {
    return { kind, message, tasks: [] };
  }

  function warned(verdict: { warnings: Array<{ kind: string; message: string; tasks: Array<string> }>; undo: { kind: string; target?: string | null } | null }) {
    const move = vi.fn().mockResolvedValue(verdict);
    return { handlers: { move, reprioritize: vi.fn(), keep: vi.fn() } as ReorderHandlers, move };
  }

  it("says nothing at all when the move was clean", async () => {
    const handlers = accepting();
    renderQueue([queued("task-a", 100), queued("task-b", 200)], { reorder: handlers });

    fireEvent.keyDown(grip("task-b"), { key: "ArrowUp", altKey: true });

    await waitFor(() => expect(handlers.moves).toHaveLength(1));
    expect(screen.queryByTestId("queue-move-notice")).not.toBeInTheDocument();
  });

  it("shows the check's own sentence, and does not block the list", async () => {
    const { handlers } = warned({ warnings: [blocked], undo: { kind: "after", target: "task-b" } });
    renderQueue([queued("task-a", 100), queued("task-b", 200), queued("task-c", 300)], {
      reorder: handlers,
    });

    fireEvent.keyDown(grip("task-c"), { key: "ArrowUp", altKey: true });

    const notice = await screen.findByTestId("queue-move-notice");
    // The rendered sentence the server wrote, not the presence of a container.
    expect(notice).toHaveTextContent(/waiting on task-a \(still open\)/);
    // Reporting, not asking: no dialog role, and every row is still reorderable.
    expect(notice).toHaveAttribute("role", "status");
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(grip("task-a")).toBeVisible();
  });

  it("lists one line per finding", async () => {
    const { handlers } = warned({
      warnings: [
        blocked,
        warning("above_prerequisite", "task-c now stands ahead of task-a, which it needs."),
        warning("demoted_blocker", "This pushed task-a (1 open task needs it) down the 'high' band."),
      ],
      undo: null,
    });
    renderQueue([queued("task-a", 100), queued("task-b", 200), queued("task-c", 300)], {
      reorder: handlers,
    });

    fireEvent.keyDown(grip("task-c"), { key: "ArrowUp", altKey: true });

    const notice = await screen.findByTestId("queue-move-notice");
    expect(notice.querySelectorAll("[data-warning]")).toHaveLength(3);
    expect(notice).toHaveTextContent("3 things worth knowing");
  });

  it("undo sends the inverse move the server named", async () => {
    const { handlers, move } = warned({
      warnings: [blocked],
      undo: { kind: "after", target: "task-b" },
    });
    renderQueue([queued("task-a", 100), queued("task-b", 200), queued("task-c", 300)], {
      reorder: handlers,
    });

    fireEvent.keyDown(grip("task-c"), { key: "ArrowUp", altKey: true });
    await screen.findByTestId("queue-move-notice");
    move.mockResolvedValue({ warnings: [], undo: null });

    fireEvent.click(screen.getByRole("button", { name: "Undo the move" }));

    await waitFor(() => expect(move).toHaveBeenCalledTimes(2));
    expect(move.mock.calls[1]).toEqual(["task-c", { after: "task-b" }]);
    await waitFor(() => expect(screen.queryByTestId("queue-move-notice")).not.toBeInTheDocument());
  });

  it("offers no undo when the server named no placement", async () => {
    const { handlers } = warned({ warnings: [blocked], undo: null });
    renderQueue([queued("task-a", 100), queued("task-b", 200), queued("task-c", 300)], {
      reorder: handlers,
    });

    fireEvent.keyDown(grip("task-c"), { key: "ArrowUp", altKey: true });

    await screen.findByTestId("queue-move-notice");
    expect(screen.queryByRole("button", { name: "Undo the move" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Keep it here" })).toBeVisible();
  });

  it("keep records the anchor and says so", async () => {
    const { handlers } = warned({ warnings: [blocked], undo: { kind: "top" } });
    renderQueue([queued("task-a", 100), queued("task-b", 200), queued("task-c", 300)], {
      reorder: handlers,
    });

    fireEvent.keyDown(grip("task-c"), { key: "ArrowUp", altKey: true });
    await screen.findByTestId("queue-move-notice");

    fireEvent.click(screen.getByRole("button", { name: "Keep it here" }));

    await waitFor(() => expect(handlers.keep).toHaveBeenCalledWith("task-c"));
    await waitFor(() => expect(screen.queryByTestId("queue-move-notice")).not.toBeInTheDocument());
    expect(screen.getByText(/anchored against automatic reordering/)).toBeInTheDocument();
  });

  it("dismissing writes nothing", async () => {
    const { handlers } = warned({ warnings: [blocked], undo: { kind: "top" } });
    renderQueue([queued("task-a", 100), queued("task-b", 200), queued("task-c", 300)], {
      reorder: handlers,
    });

    fireEvent.keyDown(grip("task-c"), { key: "ArrowUp", altKey: true });
    await screen.findByTestId("queue-move-notice");

    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));

    await waitFor(() => expect(screen.queryByTestId("queue-move-notice")).not.toBeInTheDocument());
    expect(handlers.keep).not.toHaveBeenCalled();
  });

  it("a later clean move clears an earlier notice", async () => {
    const { handlers, move } = warned({ warnings: [blocked], undo: { kind: "top" } });
    renderQueue([queued("task-a", 100), queued("task-b", 200), queued("task-c", 300)], {
      reorder: handlers,
    });

    fireEvent.keyDown(grip("task-c"), { key: "ArrowUp", altKey: true });
    await screen.findByTestId("queue-move-notice");
    move.mockResolvedValue({ warnings: [], undo: null });

    fireEvent.keyDown(grip("task-b"), { key: "ArrowUp", altKey: true });

    await waitFor(() => expect(move).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.queryByTestId("queue-move-notice")).not.toBeInTheDocument());
  });
});

describe("TaskList notice ordering", () => {
  /**
   * Two presses is one gesture, and their requests need not finish in order.
   *
   * Found in a browser, not here: promoting a blocked task twice in quick succession
   * showed the *first* move's single finding and dropped the second move's three,
   * because the earlier response landed last and overwrote the later one. The screen
   * then described a queue state that no longer existed.
   */
  it("drops a verdict that arrives after a later move has already started", async () => {
    const first = { warnings: [{ kind: "no_op", message: "First move.", tasks: [] }], undo: null };
    const second = {
      warnings: [{ kind: "promoted_unclaimable", message: "Second move.", tasks: [] }],
      undo: null,
    };
    let releaseFirst: () => void = () => {};
    const move = vi
      .fn()
      .mockImplementationOnce(
        () => new Promise((resolve) => { releaseFirst = () => resolve(first); }),
      )
      .mockImplementationOnce(() => Promise.resolve(second));
    const reorder: ReorderHandlers = { move, reprioritize: vi.fn(), keep: vi.fn() };
    renderQueue([queued("task-a", 100), queued("task-b", 200), queued("task-c", 300)], { reorder });

    fireEvent.keyDown(grip("task-c"), { key: "ArrowUp", altKey: true });
    fireEvent.keyDown(grip("task-c"), { key: "ArrowUp", altKey: true });
    await waitFor(() => expect(move).toHaveBeenCalledTimes(2));
    await screen.findByText("Second move.");

    // The first move's answer, arriving late. It must not replace what is on screen.
    releaseFirst();
    await waitFor(() => expect(screen.getByText("Second move.")).toBeVisible());
    expect(screen.queryByText("First move.")).not.toBeInTheDocument();
  });

  it("drops a late failure too, rather than reporting a move that succeeded", async () => {
    let rejectFirst: (error: Error) => void = () => {};
    const move = vi
      .fn()
      .mockImplementationOnce(
        () => new Promise((_resolve, reject) => { rejectFirst = reject; }),
      )
      .mockImplementationOnce(() => Promise.resolve({ warnings: [], undo: null }));
    const reorder: ReorderHandlers = { move, reprioritize: vi.fn(), keep: vi.fn() };
    renderQueue([queued("task-a", 100), queued("task-b", 200), queued("task-c", 300)], { reorder });

    fireEvent.keyDown(grip("task-c"), { key: "ArrowUp", altKey: true });
    fireEvent.keyDown(grip("task-c"), { key: "ArrowUp", altKey: true });
    await waitFor(() => expect(move).toHaveBeenCalledTimes(2));

    rejectFirst(new Error("409"));
    await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
    expect(renderedOrder()).toEqual(["task-c", "task-a", "task-b"]);
  });
});

// ---------------------------------------------------------------------------
// task-238 -- the same list as a master column: a tree you select from
// ---------------------------------------------------------------------------

/**
 * What these are, and what they are deliberately not.
 *
 * jsdom lays nothing out, so nothing here is evidence about a 320px column, about
 * `scrollIntoView`, or about a gesture a hand makes -- those are `e2e/task-tree.spec.ts`
 * and the by-hand check on the record. What jsdom *can* settle is the arithmetic and
 * the wiring: which rows are drawn, what the disclosure announces, where an arrow key
 * lands, what survives a refetch and a remount, and that folding a parent changes
 * nothing about where Alt+Up puts a task.
 *
 * The reorder tests below re-press without re-focusing, for the reason task-207 records:
 * a test that focuses the handle before every keypress cannot see focus being lost.
 */

function epic() {
  return [
    queued("task-parent", 100),
    task("task-child-a", { parent: "task-parent", priority: "high", queue_position: 150 }),
    task("task-child-b", { parent: "task-parent", priority: "high", queue_position: 160 }),
    queued("task-after", 200),
  ];
}

function renderTree(
  tasks: Array<TaskRead>,
  options: { entry?: string; reorder?: ReorderHandlers | null } = {},
) {
  return render(
    <MemoryRouter initialEntries={[options.entry ?? "/p/inbox/tasks"]}>
      <TaskList
        tasks={tasks}
        projectId="inbox"
        variant="tree"
        reorder={options.reorder ?? null}
      />
      <LocationProbe />
    </MemoryRouter>,
  );
}

/** The disclosure on a parent row, by what it currently offers to do. */
function fold(taskId: string) {
  return screen.getByRole("button", { name: new RegExp(`^(Fold|Unfold) ${taskId},`) });
}

function rowLink(taskId: string) {
  return document.getElementById(`task-row-${taskId}`) as HTMLElement;
}

function currentPath() {
  return screen.getByTestId("location").textContent ?? "";
}

describe("TaskList as a sidebar tree", () => {
  beforeEach(() => window.localStorage.clear());

  it("groups children under their parent at the default filter, where the table is flat", () => {
    // The table flattens as soon as any filter is set, and `open` is a filter -- so the
    // wide list has never grouped at its own default. The tree groups the *matching*
    // tasks instead, which is what lets it stay a tree here.
    renderTree(epic());

    expect(renderedOrder()).toEqual(["task-parent", "task-child-a", "task-child-b", "task-after"]);
    expect(rowFor("task-child-a").getAttribute("data-depth")).toBe("1");
    expect(rowFor("task-parent").getAttribute("data-depth")).toBe("0");
  });

  it("starts unfolded, and the disclosure says what folding would hide", () => {
    renderTree(epic());

    const control = fold("task-parent");
    expect(control).toHaveAttribute("aria-expanded", "true");
    expect(control).toHaveAccessibleName("Fold task-parent, 2 sub-tasks, 2 open");
  });

  it("folds and unfolds from the pointer, and says so out loud", () => {
    renderTree(epic());

    fireEvent.click(fold("task-parent"));

    expect(renderedOrder()).toEqual(["task-parent", "task-after"]);
    expect(fold("task-parent")).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByText("task-parent folded, hiding 2 sub-tasks, 2 open.")).toBeInTheDocument();

    fireEvent.click(fold("task-parent"));
    expect(renderedOrder()).toEqual(["task-parent", "task-child-a", "task-child-b", "task-after"]);
  });

  it("folds with Left and unfolds with Right, as a tree does", () => {
    renderTree(epic());

    fireEvent.keyDown(rowLink("task-parent"), { key: "ArrowLeft" });
    expect(renderedOrder()).toEqual(["task-parent", "task-after"]);

    fireEvent.keyDown(rowLink("task-parent"), { key: "ArrowRight" });
    expect(renderedOrder()).toEqual(["task-parent", "task-child-a", "task-child-b", "task-after"]);
  });

  it("counts every descendant a fold hides, not just the direct children", () => {
    // Folding the top of a three-deep epic hides the grandchild too. A badge reading
    // "1 sub-task" over two hidden rows is the silent hiding this exists to prevent.
    renderTree([
      queued("task-top", 100),
      task("task-mid", { parent: "task-top", priority: "high", queue_position: 110 }),
      task("task-leaf", { parent: "task-mid", priority: "high", queue_position: 120 }),
    ]);

    fireEvent.click(fold("task-top"));

    expect(renderedOrder()).toEqual(["task-top"]);
    expect(within(rowFor("task-top")).getByText("2 folded, 2 open")).toBeVisible();
    expect(fold("task-top")).toHaveAccessibleName("Unfold task-top, 2 sub-tasks, 2 open");
  });

  it("keeps a fold across a refetch and across leaving the list and coming back", () => {
    // Two different mechanisms, and this row exists to prove both. A refetch replaces
    // the task array and must not disturb the fold; a navigation unmounts the list
    // entirely, which is why the fold is written to storage rather than held in state.
    const { rerender, unmount } = renderTree(epic());
    fireEvent.click(fold("task-parent"));
    expect(renderedOrder()).toEqual(["task-parent", "task-after"]);

    // A live update: the same tasks as new objects, which is how react-query hands
    // them over.
    rerender(
      <MemoryRouter initialEntries={["/p/inbox/tasks"]}>
        <TaskList tasks={epic().map((entry) => ({ ...entry }))} projectId="inbox" variant="tree" />
        <LocationProbe />
      </MemoryRouter>,
    );
    expect(renderedOrder()).toEqual(["task-parent", "task-after"]);

    unmount();
    renderTree(epic());
    expect(renderedOrder()).toEqual(["task-parent", "task-after"]);
    expect(fold("task-parent")).toHaveAttribute("aria-expanded", "false");
  });

  it("keeps one project's folds out of another's", () => {
    renderTree(epic());
    fireEvent.click(fold("task-parent"));
    expect(renderedOrder()).toEqual(["task-parent", "task-after"]);

    cleanup();
    render(
      <MemoryRouter initialEntries={["/p/other/tasks"]}>
        <TaskList tasks={epic()} projectId="other" variant="tree" />
      </MemoryRouter>,
    );

    expect(renderedOrder()).toEqual(["task-parent", "task-child-a", "task-child-b", "task-after"]);
  });

  it("unfolds a deep link's ancestors instead of selecting something invisible", () => {
    renderTree(epic());
    fireEvent.click(fold("task-parent"));
    expect(renderedOrder()).toEqual(["task-parent", "task-after"]);
    cleanup();

    // The fold is in storage and the URL points inside it, which is what pasting a link
    // to a child of a folded epic into a fresh tab does.
    renderTree(epic(), { entry: "/p/inbox/tasks/task-child-b" });

    expect(renderedOrder()).toEqual(["task-parent", "task-child-a", "task-child-b", "task-after"]);
    expect(rowLink("task-child-b")).toHaveAttribute("aria-current", "page");
  });

  it("leaves the fold alone when a reader folds the parent of the task they are reading", () => {
    // The unfold is keyed on the *selection* changing, not on the fold state. An effect
    // that simply reconciled the two would spring the parent back open under the hand
    // that had just closed it.
    renderTree(epic(), { entry: "/p/inbox/tasks/task-child-b" });

    fireEvent.click(fold("task-parent"));

    expect(renderedOrder()).toEqual(["task-parent", "task-after"]);
  });

  it("marks the open record's row as current, and keeps it across a refetch", () => {
    const { rerender } = renderTree(epic(), { entry: "/p/inbox/tasks/task-child-a" });

    expect(rowLink("task-child-a")).toHaveAttribute("aria-current", "page");
    expect(rowLink("task-after")).not.toHaveAttribute("aria-current");
    expect(rowFor("task-child-a").getAttribute("data-selected")).toBe("true");

    rerender(
      <MemoryRouter initialEntries={["/p/inbox/tasks/task-child-a"]}>
        <TaskList tasks={epic().map((entry) => ({ ...entry }))} projectId="inbox" variant="tree" />
        <LocationProbe />
      </MemoryRouter>,
    );
    expect(rowLink("task-child-a")).toHaveAttribute("aria-current", "page");
  });

  it("moves the selection with Up and Down, and takes focus with it", () => {
    renderTree(epic(), { entry: "/p/inbox/tasks/task-parent" });

    fireEvent.keyDown(rowLink("task-parent"), { key: "ArrowDown" });

    expect(currentPath()).toBe("/p/inbox/tasks/task-child-a");
    expect(rowLink("task-child-a")).toHaveAttribute("aria-current", "page");
    // Focus follows, so the *next* press moves from the row that was just selected
    // rather than from the row the reader started on.
    expect(document.activeElement).toBe(rowLink("task-child-a"));

    fireEvent.keyDown(document.activeElement as HTMLElement, { key: "ArrowUp" });
    expect(currentPath()).toBe("/p/inbox/tasks/task-parent");
  });

  it("steps over a folded subtree rather than into it", () => {
    renderTree(epic(), { entry: "/p/inbox/tasks/task-parent" });
    fireEvent.click(fold("task-parent"));

    fireEvent.keyDown(rowLink("task-parent"), { key: "ArrowDown" });

    expect(currentPath()).toBe("/p/inbox/tasks/task-after");
  });

  it("does nothing at either end of the list", () => {
    renderTree(epic(), { entry: "/p/inbox/tasks/task-parent" });

    fireEvent.keyDown(rowLink("task-parent"), { key: "ArrowUp" });

    expect(currentPath()).toBe("/p/inbox/tasks/task-parent");
  });

  it("still steps a task through its band when the sibling above it is hidden", () => {
    // Collision case 2, settled: a step is computed over the band by `queueOrder.ts`
    // and never over the rows on screen, so folding changes what a reader sees and
    // nothing at all about where Alt+Up puts a task. The live region is what makes the
    // move legible when the neighbour it passed is not rendered.
    const handlers = accepting();
    renderTree(epic(), { reorder: handlers });
    fireEvent.click(fold("task-parent"));
    expect(renderedOrder()).toEqual(["task-parent", "task-after"]);

    fireEvent.keyDown(grip("task-after"), { key: "ArrowUp", altKey: true });

    // task-child-b is the row above it *in the band*, and is not on screen at all.
    expect(handlers.moves).toEqual([["task-after", { before: "task-child-b" }]]);
    expect(
      screen.getByText("task-after moved ahead of task-child-b in the high band."),
    ).toBeInTheDocument();
  });

  it("keeps focus on the handle, so a second Alt+Up moves the same task", async () => {
    const handlers = accepting();
    renderTree([queued("task-a", 100), queued("task-b", 200), queued("task-c", 300)], {
      reorder: handlers,
    });

    grip("task-c").focus();
    fireEvent.keyDown(grip("task-c"), { key: "ArrowUp", altKey: true });
    await waitFor(() => expect(renderedOrder()).toEqual(["task-a", "task-c", "task-b"]));
    expect(document.activeElement).toBe(grip("task-c"));

    // Pressed again without touching anything, which is the only way to see this.
    fireEvent.keyDown(document.activeElement as HTMLElement, { key: "ArrowUp", altKey: true });
    await waitFor(() => expect(renderedOrder()).toEqual(["task-c", "task-a", "task-b"]));
    expect(handlers.moves.map(([id]) => id)).toEqual(["task-c", "task-c"]);
  });

  it("treats a drop on a folded parent as an ordinary move, never a reparent", async () => {
    // Collision case 3, settled. Not "nothing", which would make a folded row a hole in
    // the list, and not unfold-on-hover, which moves every row under the pointer
    // mid-gesture. Reparenting is a real feature and its own task.
    const handlers = accepting();
    renderTree(epic(), { reorder: handlers });
    fireEvent.click(fold("task-parent"));

    fireEvent.dragStart(grip("task-after"));
    fireEvent.drop(rowFor("task-parent"));

    await waitFor(() => expect(handlers.moves).toHaveLength(1));
    expect(handlers.moves[0]).toEqual(["task-after", { before: "task-parent" }]);
    // The parent is where it was, still folded, and still nobody's parent but its own.
    expect(fold("task-parent")).toHaveAttribute("aria-expanded", "false");
  });

  it("draws a child whose parent the filter removed at the root, and says where it came from", () => {
    renderTree([
      task("task-closed-parent", {
        lifecycle: "closed",
        ball: null,
        ball_reason: null,
        outcome: "completed",
        display_status: "Completed",
      }),
      task("task-orphan", { parent: "task-closed-parent" }),
    ]);

    expect(renderedOrder()).toEqual(["task-orphan"]);
    expect(rowFor("task-orphan").getAttribute("data-depth")).toBe("0");
    expect(screen.getByText("part of task-closed-parent")).toBeVisible();
  });

  /**
   * task-362 took the number off the row and left it where it informs an action.
   *
   * The assertion is absence rather than a deleted test, because "the sidebar does not
   * print `queue_position`" is the requirement -- a test that simply stopped looking
   * would pass again the day somebody put it back. The seam and the accessible name are
   * asserted in the same place so the removal cannot quietly take either with it.
   */
  it("keeps its place in line off the row, in the seam and in the grip's name", () => {
    renderTree(epic(), { reorder: accepting() });

    const row = rowFor("task-after");
    expect(row.getAttribute("data-queue-position")).toBe("200");
    expect(row.querySelector('[data-field="queue"]')).toBeNull();
    expect(row.textContent).not.toContain("200");
    expect(grip("task-after")).toHaveAttribute(
      "aria-label",
      "Reorder task-after, high band, position 200",
    );
  });
});

describe("TaskList selection keeps the filters", () => {
  beforeEach(() => window.localStorage.clear());

  /**
   * Opening a task used to drop the query string, and beside the record that is not
   * cosmetic.
   *
   * Found in Chromium, pressing Down twice: the first press emptied the search, the
   * list re-rendered from three rows to the whole backlog, the row that had just been
   * selected was reinserted somewhere else, the browser dropped focus from the node it
   * moved -- and the second press did nothing at all. Exactly task-207's failure
   * reached through a different door.
   */
  it("carries the search and the filters into the row's own link", () => {
    render(
      <MemoryRouter initialEntries={["/p/inbox/tasks?q=058&status=all&priority=high"]}>
        <TaskList tasks={[task("task-058", { priority: "high" })]} projectId="inbox" variant="tree" />
      </MemoryRouter>,
    );

    const href = (document.getElementById("task-row-task-058") as HTMLAnchorElement).getAttribute(
      "href",
    );
    expect(href).toContain("/p/inbox/tasks/task-058");
    expect(href).toContain("q=058");
    expect(href).toContain("status=all");
    expect(href).toContain("priority=high");
  });

  it("leaves the full-width table's links exactly as they were", () => {
    // The split is deliberate rather than incidental. The table has no selection to
    // lose and no list left on screen to re-render, so it keeps the bare path it has
    // always had -- which is this task's constraint: below the device-class threshold
    // the list renders as it does today.
    render(
      <MemoryRouter initialEntries={["/p/inbox/tasks?q=058&status=all"]}>
        <TaskList tasks={[task("task-058")]} projectId="inbox" />
      </MemoryRouter>,
    );

    const link = screen.getByRole("link", { name: /task-058/ });
    expect(link).toHaveAttribute("href", "/p/inbox/tasks/task-058");
  });

  it("carries them through an arrow-key selection too", () => {
    render(
      <MemoryRouter initialEntries={["/p/inbox/tasks/task-a?q=queue&status=all"]}>
        <TaskList
          tasks={[task("task-a", { title: "queue one" }), task("task-b", { title: "queue two" })]}
          projectId="inbox"
          variant="tree"
        />
        <LocationProbe />
      </MemoryRouter>,
    );

    fireEvent.keyDown(document.getElementById("task-row-task-a") as HTMLElement, {
      key: "ArrowDown",
    });

    const url = screen.getByTestId("location").textContent ?? "";
    expect(url).toContain("/p/inbox/tasks/task-b");
    expect(url).toContain("q=queue");
    expect(url).toContain("status=all");
  });
});

// ---------------------------------------------------------------------------
// task-385 -- the filter button is an icon, and the keyboard help is behind a `?`
// ---------------------------------------------------------------------------

function helpButton() {
  return screen.getByRole("button", { name: "Keyboard shortcuts" });
}

function keyboardHelp() {
  return screen.getByTestId("keyboard-help");
}

/**
 * Hover is not asserted here: jsdom has no pointer to rest on a button, and a
 * synthesised `pointerenter` would test the synthesis. `e2e/filter-popover.spec.ts`
 * hovers for real. What jsdom can settle is the click, the dismissals and the wiring.
 */
describe("TaskList keyboard help and filter icon", () => {
  it("draws the filter button as an icon while its name still says Filters", () => {
    renderTree(epic());

    const button = filterButton();
    expect(button).toHaveAccessibleName("Filters, none set");
    expect(within(button).getByTestId("filter-icon")).toBeInTheDocument();
    // The word is gone from the drawing: the space it took is the point of the task.
    expect(button.textContent).toBe("");
  });

  it("keeps the help off screen until the button is pressed", () => {
    renderTree(epic(), { reorder: accepting() });

    expect(helpButton()).toHaveAttribute("aria-expanded", "false");
    expect(keyboardHelp()).toHaveClass("sr-only");
    expect(keyboardHelp()).toHaveAttribute("data-open", "false");

    fireEvent.click(helpButton());

    expect(helpButton()).toHaveAttribute("aria-expanded", "true");
    expect(keyboardHelp()).not.toHaveClass("sr-only");
    expect(keyboardHelp()).toHaveTextContent(/fold, remembered for this project/);

    fireEvent.click(helpButton());

    expect(keyboardHelp()).toHaveClass("sr-only");
  });

  it("still describes every grip while it is closed", () => {
    // Hiding the words from sight must not take them from a screen reader: the
    // description a grip announces cannot depend on whether somebody hovered first.
    renderTree(epic(), { reorder: accepting() });

    const described = grip("task-after").getAttribute("aria-describedby") ?? "";
    expect(described).toBe(keyboardHelp().id);
    expect(keyboardHelp()).toHaveTextContent(/step a task through its priority band/);
  });

  it("closes on Escape, returning focus only if focus was inside", () => {
    renderTree(epic());
    helpButton().focus();
    fireEvent.click(helpButton());

    fireEvent.keyDown(document, { key: "Escape" });

    expect(keyboardHelp()).toHaveClass("sr-only");
    expect(document.activeElement).toBe(helpButton());
  });

  it("closes on a click outside it and stays open for a click inside it", () => {
    renderTree(epic());
    fireEvent.click(helpButton());

    fireEvent.mouseDown(keyboardHelp());
    expect(keyboardHelp()).not.toHaveClass("sr-only");

    fireEvent.mouseDown(rowLink("task-after"));
    expect(keyboardHelp()).toHaveClass("sr-only");
  });

  it("offers the queue-order help on the wide list, and no button where there is none", () => {
    renderQueue([queued("task-a", 100)], { reorder: accepting() });
    expect(keyboardHelp()).toHaveTextContent(/Rows are in queue order/);
    cleanup();

    // The wide list without reordering has no keys to explain.
    renderList([task("task-open")]);
    expect(screen.queryByRole("button", { name: "Keyboard shortcuts" })).not.toBeInTheDocument();
  });
});

/**
 * Priority is where a row sits, not a chip on it (task-563).
 *
 * Every assertion reads what a browser receives -- the header's text, the order of the
 * list items, which element the status chip is inside -- rather than the presence of a
 * class or an attribute.
 */
describe("TaskList band headers", () => {
  const closed = {
    lifecycle: "closed" as const,
    ball: null,
    ball_reason: null,
    outcome: "completed" as const,
    display_status: "Completed",
    status_category: "closed" as const,
  };

  function bands() {
    return [
      queued("task-crit", 100, "critical"),
      task("task-crit-child", { parent: "task-crit", priority: "medium", queue_position: 300 }),
      queued("task-high-a", 100, "high"),
      queued("task-high-b", 200, "high"),
      queued("task-low", 100, "low"),
    ];
  }

  /** Every list item in the sidebar, in order, as a reader meets it. */
  function items() {
    return Array.from(document.querySelectorAll("ul > li")).map((item) =>
      item.hasAttribute("data-band-header")
        ? `# ${item.textContent}`
        : (item.getAttribute("data-task") ?? "?"),
    );
  }

  it("opens each band with a header and skips a band with no tasks", () => {
    renderTree(bands());

    expect(items()).toEqual([
      "# CRITICAL TASKS",
      "task-crit",
      "task-crit-child",
      "# HIGH TASKS",
      "task-high-a",
      "task-high-b",
      "# LOW TASKS",
      "task-low",
    ]);
    expect(screen.getAllByRole("heading", { level: 3 }).map((h) => h.textContent)).toEqual([
      "CRITICAL TASKS",
      "HIGH TASKS",
      "LOW TASKS",
    ]);
  });

  it("keeps a child of another priority under its parent, with no priority on any row", () => {
    renderTree(bands());

    const child = document.querySelector('[data-task="task-crit-child"]') as HTMLElement;
    expect(child).toHaveAttribute("data-depth", "1");
    for (const row of document.querySelectorAll("li[data-task]")) {
      // What the row says besides its own id and title, which here contain band words.
      const id = row.getAttribute("data-task") ?? "";
      const rest = (row.textContent ?? "").replace(`Title of ${id}`, "").replace(id, "");
      expect(rest).not.toMatch(/critical|high|medium|low/i);
      expect(row.querySelector("[data-priority]")).toBeNull();
    }
  });

  it("gives a header no link, no grip and no place in arrow-key navigation", () => {
    renderTree(bands(), { entry: "/p/inbox/tasks/task-crit-child" });

    for (const header of document.querySelectorAll("li[data-band-header]")) {
      expect(header.querySelector("a, button")).toBeNull();
      expect(header).not.toHaveAttribute("draggable");
    }
    // Down from the last critical row lands on the first high task, not on the header.
    fireEvent.keyDown(rowLink("task-crit-child"), { key: "ArrowDown" });
    expect(currentPath()).toBe("/p/inbox/tasks/task-high-a");
  });

  it("keeps the headers in a filtered view, and never adds a priority chip there", () => {
    renderTree(bands(), { entry: "/p/inbox/tasks?q=high" });

    expect(items()).toEqual(["# HIGH TASKS", "task-high-a", "task-high-b"]);
    expect(document.querySelector("li[data-task] [data-priority]")).toBeNull();
  });

  it("separates closed work from the open band of the same priority", () => {
    renderTree(
      [queued("task-open-high", 100, "high"), task("task-done-high", { ...closed, priority: "high" })],
      { entry: "/p/inbox/tasks?status=all" },
    );

    expect(items()).toEqual([
      "# HIGH TASKS",
      "task-open-high",
      "# HIGH TASKS · CLOSED",
      "task-done-high",
    ]);
  });

  it("carries each header's colour down the left edge of every row in its band", () => {
    renderTree(bands());

    const edge = (selector: string) =>
      (document.querySelector(selector) as HTMLElement).style.borderLeftColor;
    const colour = (hex: string) => {
      const probe = document.createElement("div");
      probe.style.color = hex;
      return probe.style.color;
    };
    const critical = colour(PRIORITY_COLOURS.critical);
    const high = colour(PRIORITY_COLOURS.high);
    expect(critical).not.toBe(high);
    // The header itself carries no edge since task-576: the colour runs down the rows.
    expect(edge('[data-band-header="critical"]')).toBe("");
    expect(edge('[data-band-header="high"]')).toBe("");
    expect(edge('[data-task="task-crit"]')).toBe(critical);
    // The medium child sits in the critical band, so it takes the critical edge.
    expect(edge('[data-task="task-crit-child"]')).toBe(critical);
    expect(edge('[data-task="task-high-a"]')).toBe(high);
    expect(edge('[data-task="task-high-b"]')).toBe(high);
  });

  it("puts the status chip on the id line and gives the title its own clamped line", () => {
    renderTree([queued("task-one", 100, "high")]);

    const idLine = rowLink("task-one").querySelector('[data-field="id-line"]') as HTMLElement;
    expect(idLine.firstElementChild).toHaveTextContent(/^task-one$/);
    const chip = within(idLine).getByText("Ready");
    expect(chip).toHaveAttribute("data-status-category", "ready");
    // The chip follows the id inside the same line.
    expect(idLine.firstElementChild!.compareDocumentPosition(chip) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();

    const title = rowLink("task-one").querySelector('[data-field="title"]') as HTMLElement;
    expect(idLine.contains(title)).toBe(false);
    expect(title).toHaveTextContent("Title of task-one");
    expect(title).toHaveAttribute("title", "Title of task-one");
    expect(title.className).toContain("line-clamp-2");
  });
});
