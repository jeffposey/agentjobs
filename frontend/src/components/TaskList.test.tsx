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

describe("TaskList queue order", () => {
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
