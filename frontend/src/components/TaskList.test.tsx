import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import type { BrokenTaskFile, Priority, QueueProblemRead, TaskRead } from "../api/types";
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

  it("keeps a superseded task distinguishable from a completed one on the list", () => {
    renderList([
      task("task-058-superseded", { lifecycle: "closed", ball: null, ball_reason: null, outcome: "superseded", display_status: "Superseded" }),
      task("task-059-completed", { lifecycle: "closed", ball: null, ball_reason: null, outcome: "completed", display_status: "Completed" }),
    ], "/p/inbox/tasks?status=closed");

    const table = screen.getByRole("region", { name: "Tasks" });
    expect(within(table).getByText("Superseded")).toBeVisible();
    expect(within(table).getByText("Completed")).toBeVisible();
    expect(within(table).queryByText("Done")).not.toBeInTheDocument();
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

  it("says what claiming an umbrella would get you", () => {
    renderList([task("task-umbrella", {
      actionable: false,
      open_children_count: 2,
    })]);

    expect(screen.getByText("Waiting on sub-tasks")).toBeVisible();
    // task-164: an epic is claimable now, and the claim is for supervision. The old
    // copy said the children "must finish first", which the claim no longer requires.
    expect(screen.getByText(/2 open sub-tasks to finish\./)).toBeVisible();
    expect(screen.getByText(/Claim it to supervise them/)).toBeVisible();
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
        brokenFiles={[]}
        projectId="inbox"
        reorder={options.reorder ?? null}
        queueProblems={options.problems ?? []}
        repairCommand="agentjobs queue repair"
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

function accepting(): ReorderHandlers & { moves: Array<[string, QueueMove]> } {
  const moves: Array<[string, QueueMove]> = [];
  return {
    moves,
    move: async (taskId, move) => {
      moves.push([taskId, move]);
    },
    reprioritize: async () => {},
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
    const reorder: ReorderHandlers = { move: vi.fn(), reprioritize: vi.fn() };
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

  it("names the broken queue and refuses to offer an order it cannot justify", () => {
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

    const banner = screen.getByRole("alert", { name: "Queue is broken" });
    expect(banner).toHaveTextContent("band 'high' position 100 is claimed by task-a, task-b");
    expect(banner).toHaveTextContent("agentjobs queue repair");
    // Every gesture places a task relative to a neighbour, and corruption is exactly
    // what makes a neighbour's position untrustworthy.
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
