import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import type {
  ArmedProjectView,
  EpicWalkView,
  LiveRunView,
  LiveRunsView,
  MachineHolderView,
  QueuedDispatchView,
  StartPauseView,
  TaskCardRead,
} from "../api/types";
import { capacitySentence } from "./LiveRuns";
import {
  BOARD_CELL_LIMIT,
  SlotBoard,
  boardLayout,
  orderedRuns,
  walkCountsSentence,
  walkState,
} from "./SlotBoard";

/**
 * The slot board (task-092), rendered from a response object.
 *
 * The layout rules are a pure function and are tested as one: how many cells there are,
 * which are busy, and what fills the rest are the decisions worth pinning, and asserting
 * them through the DOM would only add a rendering to every failure message. What the
 * DOM tests cover is what a person's browser actually acts on -- the health word, the
 * elapsed time, the link a foreign run points at, and the button on a free cell.
 */

// `holds_slot` mirrors the server's own rule here so that a fixture naming only a mode
// is still a run the server could have sent. A test about the released slot (task-482)
// sets it explicitly, which is the whole point of it being a field rather than a
// derivation.
function run(overrides: Partial<LiveRunView> = {}): LiveRunView {
  const mode = overrides.mode ?? "session";
  return {
    run_id: "run_a",
    task_id: "task-001",
    task_title: "Teach the queue to count",
    project_id: "alpha",
    project_name: "Alpha Project",
    mode: "session",
    session: true,
    posture: "auto",
    status: "running",
    health: "working",
    started_at: "2026-09-04T01:00:00Z",
    elapsed_seconds: 90,
    task_url: "/p/alpha/tasks/task-001",
    output_url: "/api/projects/alpha/dispatch/runs/run_a/output",
    holds_slot: mode !== "interactive" && mode !== "walk",
    ...overrides,
  };
}

function holder(overrides: Partial<MachineHolderView> = {}): MachineHolderView {
  return {
    kind: "finish",
    lock_name: "task-002",
    task_id: "task-002",
    task_title: "Something merging",
    project_id: "alpha",
    project_name: "Alpha Project",
    finish_id: "fin_abc",
    pid: 1234,
    started_at: "2026-09-04T01:00:00Z",
    elapsed_seconds: 30,
    detail: "running the gate",
    task_url: "/p/alpha/tasks/task-002",
    ...overrides,
  };
}

function body(overrides: Partial<LiveRunsView> = {}): LiveRunsView {
  return {
    occupied: 0,
    max_concurrent_runs: 3,
    dispatch_configured: true,
    runs: [],
    holders: [],
    generated_at: "2026-09-04T01:01:30Z",
    ...overrides,
  };
}

function queued(overrides: Partial<QueuedDispatchView> = {}): QueuedDispatchView {
  return {
    queue_id: "q_aaaa",
    position: 1,
    task_id: "task-050",
    task_title: "Waiting for a slot",
    project_id: "alpha",
    project_name: "Alpha Project",
    queued_at: "2026-09-04T01:00:00Z",
    waiting_seconds: 120,
    queued_by: "Jeff Posey",
    source: "manual",
    status: "queued",
    detail: "",
    task_url: "/p/alpha/tasks/task-050",
    ...overrides,
  };
}

function task(id: string, overrides: Partial<TaskCardRead> = {}): TaskCardRead {
  return {
    schema: 2,
    id,
    title: `Title of ${id}`,
    created: "2026-09-01T08:00:00Z",
    updated: "2026-09-01T09:00:00Z",
    lifecycle: "ready",
    ball: "agent",
    ball_reason: "available",
    display_status: "Ready",
    priority: "medium",
    category: "general",
    summary: `Summary of ${id}`,
    can_brief: true,
    ...overrides,
  };
}

function renderBoard(node: React.ReactNode) {
  return render(<MemoryRouter initialEntries={["/p/alpha"]}>{node}</MemoryRouter>);
}

const cellStates = () =>
  screen.getAllByTestId("slot-cell").map((cell) => cell.dataset.slotState);

describe("how many cells the board has", () => {
  it.each([1, 2, 3, 5])("draws exactly %i cells at that ceiling", (ceiling) => {
    // ac-1: the count is the machine's answer, not a number the browser worked out.
    // Asserted at more than one ceiling because a board that hard-coded three would
    // pass a single-value test.
    const layout = boardLayout(body({ max_concurrent_runs: ceiling }), [], "alpha");

    expect(layout.cells).toHaveLength(ceiling);
  });

  it("is a single panel at a ceiling of one", () => {
    const layout = boardLayout(
      body({ max_concurrent_runs: 1, occupied: 1, runs: [run()] }),
      [task("task-next")],
      "alpha",
    );

    expect(layout.cells.map((cell) => cell.kind)).toEqual(["run"]);
  });

  it("counts busy cells off `occupied`, not off the runs it was shown", () => {
    // `occupied` is machine-wide and includes projects this caller may not see
    // (task-333). Drawing the difference as free would offer a Dispatch button on a
    // slot the concurrency guard knows is taken -- the browser disagreeing with the
    // guard, which is the one thing the endpoint's docstring forbids.
    const layout = boardLayout(body({ occupied: 2, runs: [run()] }), [task("task-next")], "alpha");

    expect(layout.cells.map((cell) => cell.kind)).toEqual(["run", "opaque", "queued"]);
  });

  it("stops at the drawing limit and says what it is not drawing", () => {
    const layout = boardLayout(
      body({ max_concurrent_runs: BOARD_CELL_LIMIT + 4, occupied: BOARD_CELL_LIMIT + 2 }),
      [],
      "alpha",
    );

    expect(layout.cells).toHaveLength(BOARD_CELL_LIMIT);
    expect(layout.hiddenSlots).toBe(4);
    expect(layout.hiddenBusy).toBe(2);
  });

  it("draws a queue rather than a board on a machine that configured no ceiling", () => {
    // `dispatch_configured: false` means the ceiling is a default nobody chose, so
    // cells drawn from it would assert a capacity that is a fiction on top of a
    // fiction. The queue is still real, so that is what it shows.
    const layout = boardLayout(
      body({ dispatch_configured: false }),
      [task("task-one"), task("task-two")],
      "alpha",
    );

    expect(layout.unconfigured).toBe(true);
    expect(layout.cells.map((cell) => cell.kind)).toEqual(["queued", "queued"]);
  });
});

describe("a machine running over its ceiling", () => {
  /**
   * task-461: `occupied` may exceed `max_concurrent_runs`, because a person chose
   * *Dispatch now* on a full machine. The board's job then is to be honest — a board of
   * slots that showed one card while two agents wrote to two repositories would be the
   * single most misleading thing on the page.
   */
  const overage = () =>
    body({
      max_concurrent_runs: 1,
      occupied: 2,
      runs: [run(), run({ run_id: "run_b", task_id: "task-002", over_ceiling: true })],
    });

  it("draws every occupied slot rather than clipping to the ceiling", () => {
    const layout = boardLayout(overage(), [task("task-next")], "alpha");

    expect(layout.cells.map((cell) => cell.kind)).toEqual(["run", "run"]);
    expect(layout.overCeiling).toBe(1);
    // Nothing is hidden and nothing is free: the extra card is not a slot the machine
    // allows, so it must not be reported as one it is not drawing either.
    expect(layout.hiddenSlots).toBe(0);
    expect(layout.hiddenBusy).toBe(0);
  });

  it("is still bounded by the drawing limit", () => {
    const layout = boardLayout(
      body({ max_concurrent_runs: 1, occupied: BOARD_CELL_LIMIT + 3 }),
      [],
      "alpha",
    );

    expect(layout.cells).toHaveLength(BOARD_CELL_LIMIT);
    expect(layout.overCeiling).toBe(BOARD_CELL_LIMIT + 2);
  });

  it("reports nothing over the ceiling on an ordinary full machine", () => {
    const layout = boardLayout(body({ max_concurrent_runs: 1, occupied: 1, runs: [run()] }), [], "alpha");

    expect(layout.overCeiling).toBe(0);
  });

  it("marks the overage run and says so in words", () => {
    renderBoard(<SlotBoard body={overage()} queue={[]} projectId="alpha" />);

    // The badge is on the card, so a reader counting two cards against a ceiling of one
    // is told which one explains the difference.
    expect(screen.getAllByTestId("slot-over-ceiling")).toHaveLength(1);
    expect(screen.getByTestId("slot-board-over-ceiling")).toHaveTextContent(
      "1 run above this machine's ceiling, started by a person who chose to.",
    );
  });

  it("counts honestly in the capacity sentence", () => {
    // "2 of 1 slots busy" is the true sentence. Clamping it would be the surface lying
    // to keep a number tidy, and the clause is what stops it reading as a counting bug.
    expect(capacitySentence(overage())).toBe("2 of 1 slot busy · 1 over the ceiling");
    expect(capacitySentence(body({ occupied: 1, max_concurrent_runs: 3 }))).toBe(
      "1 of 3 slots busy",
    );
  });
});

describe("what fills the free cells", () => {
  it("offers a different task in each one, in queue order", () => {
    // ac-3. The failure this rules out is the obvious implementation: `next_task`
    // repeated into every free cell.
    const layout = boardLayout(body(), [task("task-one"), task("task-two"), task("task-three")], "alpha");

    expect(
      layout.cells.map((cell) => (cell.kind === "queued" ? cell.task.id : cell.kind)),
    ).toEqual(["task-one", "task-two", "task-three"]);
  });

  it("says a slot is empty rather than repeating the one task it has", () => {
    const layout = boardLayout(body(), [task("task-one")], "alpha");

    expect(layout.cells.map((cell) => cell.kind)).toEqual(["queued", "empty", "empty"]);
  });

  it("keeps every cell while an alarm holds the page, and withholds only the action", () => {
    // ac-7 / task-081, in its precise form: an alarm must never compete with a *call to
    // action*. A cell that names a task and links to it is not one -- the Active Tasks
    // list below the board has been doing exactly that since before the ladder existed
    // -- so the board keeps its shape and loses its buttons. Dropping the cells instead
    // would hide the board on every day something is waiting on the reader, which on
    // this project is most of them.
    renderBoard(
      <SlotBoard
        body={body({ occupied: 1, runs: [run()] })}
        queue={[task("task-one")]}
        projectId="alpha"
        statusOnly
        renderQueueAction={(queued) => <button type="button">Dispatch {queued.id}</button>}
        renderQueueGate={() => <p>Dispatch is switched off for this project.</p>}
      />,
    );

    expect(cellStates()).toEqual(["run", "queued", "empty"]);
    expect(screen.getByText("Title of task-one")).toBeVisible();
    expect(screen.queryByRole("button", { name: /Dispatch/ })).not.toBeInTheDocument();
    expect(screen.queryByText(/switched off/)).not.toBeInTheDocument();
  });

  it("offers no way to create work from an empty cell while an alarm holds the page", () => {
    renderBoard(<SlotBoard body={body()} queue={[]} projectId="alpha" statusOnly />);

    expect(cellStates()).toEqual(["empty", "empty", "empty"]);
    expect(screen.queryByRole("link", { name: /Create task/ })).not.toBeInTheDocument();
  });
});

describe("cell order across polls", () => {
  it("orders occupied cells by when their run started", () => {
    const first = run({ run_id: "run_b", started_at: "2026-09-04T01:00:00Z" });
    const second = run({ run_id: "run_a", started_at: "2026-09-04T02:00:00Z" });

    // The ledger's own scan order is not an order. Sorting on anything the poll
    // recomputes -- health, elapsed -- would permute the board every two seconds.
    expect(orderedRuns([second, first]).map((item) => item.run_id)).toEqual(["run_b", "run_a"]);
    expect(orderedRuns([first, second]).map((item) => item.run_id)).toEqual(["run_b", "run_a"]);
  });

  it("sorts a run with no start time last rather than randomly", () => {
    const dated = run({ run_id: "run_a" });
    const undated = run({ run_id: "run_b", started_at: null });

    expect(orderedRuns([undated, dated]).map((item) => item.run_id)).toEqual(["run_a", "run_b"]);
  });

  it("does not reorder the cells that did not change when a run starts or ends", () => {
    // ac-4, said as the sequence a person actually watches: two runs, a third starts,
    // the first ends. The cells that survive keep their order and their keys.
    const one = run({ run_id: "run_1", started_at: "2026-09-04T01:00:00Z" });
    const two = run({ run_id: "run_2", started_at: "2026-09-04T02:00:00Z" });
    const three = run({ run_id: "run_3", started_at: "2026-09-04T03:00:00Z" });
    const queue = [task("task-one"), task("task-two"), task("task-three")];

    const keys = (runs: LiveRunView[]) =>
      boardLayout(body({ occupied: runs.length, runs }), queue, "alpha").cells.map(
        (cell) => cell.key,
      );

    expect(keys([one, two])).toEqual(["run_1", "run_2", "task-one"]);
    // A third starts. The two already going keep their cells; the free cell is taken.
    expect(keys([one, two, three])).toEqual(["run_1", "run_2", "run_3"]);
    // The oldest ends. The other two keep their relative order rather than shuffling.
    expect(keys([two, three])).toEqual(["run_2", "run_3", "task-one"]);
  });
});

describe("the board a person reads", () => {
  it("shows a run's health, its elapsed time and a link into its own project", () => {
    // ac-2. `task_url` is built by the server precisely so a run belonging to another
    // project links into that project rather than into the one being viewed.
    renderBoard(
      <SlotBoard
        body={body({
          occupied: 1,
          runs: [run({ health: "parked", elapsed_seconds: 3_600, project_id: "beta", project_name: "Beta Project", task_url: "/p/beta/tasks/task-001" })],
        })}
        queue={[]}
        projectId="alpha"
      />,
    );

    const cell = screen.getAllByTestId("slot-cell")[0]!;
    // The health *vocabulary*, not the ledger's word: "live" does not mean "working".
    expect(within(cell).getByText("Waiting on you")).toBeVisible();
    expect(cell).toHaveTextContent("1h 00m");
    expect(cell).toHaveTextContent("Beta Project");
    expect(within(cell).getByRole("link")).toHaveAttribute("href", "/p/beta/tasks/task-001");
  });

  it("names the machine's capacity from the body rather than from the cells", () => {
    renderBoard(
      <SlotBoard body={body({ occupied: 2, runs: [run()] })} queue={[]} projectId="alpha" />,
    );

    expect(screen.getByTestId("slot-board-capacity")).toHaveTextContent("2 of 3 slots busy");
  });

  it("puts the page's Dispatch control on each free cell and the disclosure on the first", () => {
    renderBoard(
      <SlotBoard
        body={body()}
        queue={[task("task-one"), task("task-two")]}
        projectId="alpha"
        renderQueueAction={(queued) => <button type="button">Dispatch {queued.id}</button>}
        renderWhyThisOne={() => <p>Because it is first in the high band.</p>}
      />,
    );

    expect(screen.getByRole("button", { name: "Dispatch task-one" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Dispatch task-two" })).toBeVisible();
    // The disclosure explains why the *winner* is the winner, so it is not repeated:
    // there is no answer to give for the second cell.
    expect(screen.getAllByText("Because it is first in the high band.")).toHaveLength(1);
    const first = screen.getAllByTestId("slot-cell")[0]!;
    expect(within(first).getByText("Because it is first in the high band.")).toBeVisible();
  });

  it("draws a finish as a card, without taking a free cell from the board", () => {
    // task-352: a finish was a footnote under three free cells, and the footnote was
    // read as nothing happening. It is a card now -- and an *extra* one, because it
    // holds no slot: every free cell and its Dispatch button is still there.
    renderBoard(
      <SlotBoard
        body={body({ holders: [holder({ detail: "gate" })] })}
        queue={[task("task-one"), task("task-two"), task("task-three")]}
        projectId="alpha"
        renderQueueAction={(item) => <button type="button">Dispatch {item.id}</button>}
      />,
    );

    expect(cellStates()).toEqual(["finish", "queued", "queued", "queued"]);
    expect(screen.getAllByRole("button", { name: /^Dispatch/ })).toHaveLength(3);
    const card = screen.getAllByTestId("slot-cell")[0]!;
    expect(within(card).getByText("Finishing")).toHaveAttribute("data-finish-step", "gate");
    expect(within(card).getByText("Running the gate")).toBeVisible();
    expect(within(card).getByText("30s")).toBeVisible();
    expect(within(card).getByRole("link", { name: /task-002/ })).toHaveAttribute(
      "href",
      "/p/alpha/tasks/task-002",
    );
    expect(screen.getByTestId("slot-board-capacity")).toHaveTextContent(
      "0 of 3 slots busy · 1 merging",
    );
    // The runway it holds is not a second thing, and no footnote is left to hold it.
    expect(screen.queryByTestId("slot-board-holders")).toBeNull();
  });

  it("keeps the runs ahead of a finish and the free cells behind it", () => {
    const layout = boardLayout(
      body({ occupied: 1, runs: [run()], holders: [holder()] }),
      [task("task-next")],
      "alpha",
    );

    expect(layout.cells.map((cell) => cell.kind)).toEqual(["run", "finish", "queued", "empty"]);
  });

  it("does not offer a task that is being finished", () => {
    const layout = boardLayout(
      body({ holders: [holder({ task_id: "task-two", project_id: "alpha" })] }),
      [task("task-two"), task("task-three")],
      "alpha",
    );

    expect(layout.cells.map((cell) => (cell.kind === "queued" ? cell.task.id : cell.kind))).toEqual(
      ["finish", "task-three", "empty", "empty"],
    );
  });

  it("renders nothing at all until the machine has answered", () => {
    // A board that painted a guessed number of cells and corrected itself one poll
    // later would be a page whose shape is a guess.
    const { container } = renderBoard(<SlotBoard body={null} queue={[]} projectId="alpha" />);

    expect(container).toBeEmptyDOMElement();
  });

  it("keeps a runway visible even when there is no board to draw", () => {
    // A runway with no finish card to explain it -- its finish record was unreadable --
    // still gets a line: every other merge in that repository is queued behind it.
    renderBoard(
      <SlotBoard
        body={body({ dispatch_configured: false, holders: [holder({ kind: "runway", task_id: "" })] })}
        queue={[]}
        projectId="alpha"
      />,
    );

    expect(screen.queryAllByTestId("slot-cell")).toHaveLength(0);
    expect(screen.getByTestId("slot-board-holders")).toHaveTextContent("merge runway");
  });

  it("draws a finish on a machine with no configured ceiling", () => {
    renderBoard(
      <SlotBoard
        body={body({ dispatch_configured: false, holders: [holder()] })}
        queue={[]}
        projectId="alpha"
      />,
    );

    expect(cellStates()).toEqual(["finish"]);
  });

  it("links to the long form on both the header and the overflow line", () => {
    renderBoard(
      <SlotBoard
        body={body({ max_concurrent_runs: BOARD_CELL_LIMIT + 2 })}
        queue={[]}
        projectId="alpha"
      />,
    );

    expect(screen.getByRole("link", { name: "Running now →" })).toHaveAttribute(
      "href",
      "/p/alpha/runs",
    );
    expect(screen.getByTestId("slot-board-overflow")).toHaveTextContent("+2 more slots");
  });
});

describe("a task never lands in two cells", () => {
  it("does not offer a task that a run in this project is already holding", () => {
    // ac-3. The two halves come from two endpoints and nothing reconciles them: a run
    // whose task was released back to `ready` while its process lives is live *and*
    // claimable, and the naive board draws it as a run and then offers a Dispatch
    // button under a second copy of it.
    const layout = boardLayout(
      body({ occupied: 1, runs: [run({ task_id: "task-one", project_id: "alpha" })] }),
      [task("task-one"), task("task-two")],
      "alpha",
    );

    expect(
      layout.cells.map((cell) =>
        cell.kind === "queued" ? cell.task.id : cell.kind === "run" ? cell.run.task_id : cell.kind,
      ),
    ).toEqual(["task-one", "task-two", "empty"]);
  });

  it("still offers a task whose id merely collides with a run in another project", () => {
    // Task ids are unique within a project, not across the machine. `task-one` here and
    // `task-one` in Elsewhere are two different tasks, and suppressing this one because
    // of that one would silently drop work off the board.
    const layout = boardLayout(
      body({ occupied: 1, runs: [run({ task_id: "task-one", project_id: "beta" })] }),
      [task("task-one")],
      "alpha",
    );

    expect(layout.cells.map((cell) => cell.kind)).toEqual(["run", "queued", "empty"]);
  });
});

describe("a session somebody is working in (task-354)", () => {
  const attended = (overrides: Partial<LiveRunView> = {}) =>
    run({
      run_id: "run_chat",
      mode: "interactive",
      session: false,
      posture: "",
      task_id: "task-352",
      task_title: "Worked in a chat window",
      task_url: "/p/alpha/tasks/task-352",
      ...overrides,
    });

  it("draws it as a card without taking a free cell from the board", () => {
    // It is in `runs` and not in `occupied`: it holds its task, not a slot. So the
    // three free cells and their Dispatch buttons are exactly what they were.
    renderBoard(
      <SlotBoard
        body={body({ runs: [attended()] })}
        queue={[task("task-one"), task("task-two"), task("task-three")]}
        projectId="alpha"
        renderQueueAction={(item) => <button type="button">Dispatch {item.id}</button>}
      />,
    );

    expect(cellStates()).toEqual(["run", "queued", "queued", "queued"]);
    expect(screen.getAllByRole("button", { name: /^Dispatch/ })).toHaveLength(3);
    expect(screen.getByTestId("slot-board-capacity")).toHaveTextContent("0 of 3 slots busy");
  });

  it("says whose session it is where a dispatched run shows its posture", () => {
    renderBoard(<SlotBoard body={body({ runs: [attended()] })} queue={[]} projectId="alpha" />);

    const card = screen.getAllByTestId("slot-cell")[0]!;
    expect(within(card).getByText("your session")).toBeVisible();
    expect(within(card).getByText("Worked in a chat window")).toBeVisible();
  });

  it("does not let it displace a dispatched run from a slot cell", () => {
    // The ordering trap: slot cells are filled from the runs that hold slots, so an
    // interactive run in the middle of the list must not be indexed into one.
    const layout = boardLayout(
      body({
        occupied: 1,
        runs: [attended({ started_at: "2026-09-04T00:00:00Z" }), run()],
      }),
      [task("task-next")],
      "alpha",
    );

    expect(layout.cells.map((cell) => (cell.kind === "run" ? cell.run.run_id : cell.kind))).toEqual(
      ["run_a", "run_chat", "queued", "empty"],
    );
  });

  it("does not offer a task its own session is working", () => {
    const layout = boardLayout(
      body({ runs: [attended({ task_id: "task-two", project_id: "alpha" })] }),
      [task("task-two"), task("task-three")],
      "alpha",
    );

    expect(layout.cells.map((cell) => (cell.kind === "queued" ? cell.task.id : cell.kind))).toEqual(
      ["run", "task-three", "empty", "empty"],
    );
  });

  it("reads an idle session as idle rather than as working", () => {
    renderBoard(
      <SlotBoard body={body({ runs: [attended({ health: "idle" })] })} queue={[]} projectId="alpha" />,
    );

    expect(screen.getByText("Idle")).toHaveAttribute("data-health", "idle");
  });
});

/**
 * The waiting rail (task-459, ac-4).
 *
 * Every assertion here is on a *value* a person or a click acts on -- the order, the
 * task it names, who queued it, and whether the cancel control is there -- rather than
 * on the presence of markup. A rail that rendered its cards in the wrong order, or
 * numbered them 1, 2, 3 over entries that are really 1, 2 and 5, would pass a test that
 * only counted nodes.
 */
describe("dispatches waiting for a slot", () => {
  const waitingBody = (queue: QueuedDispatchView[]) =>
    body({ occupied: 1, max_concurrent_runs: 1, runs: [run()], queued: queue, queue_limit: 20 });

  it("lists them in the order the server says they will start", () => {
    renderBoard(
      <SlotBoard
        body={waitingBody([
          queued({ queue_id: "q_1", position: 1, task_id: "task-050" }),
          queued({ queue_id: "q_2", position: 2, task_id: "task-051" }),
        ])}
        queue={[]}
        projectId="alpha"
      />,
    );

    const cards = screen.getAllByTestId("queued-dispatch");
    expect(cards.map((card) => card.dataset.taskId)).toEqual(["task-050", "task-051"]);
    // The server's number, not the array index: the rows are filtered by what this
    // caller may see, so counting them here would promise somebody a place they do not
    // have.
    expect(cards.map((card) => card.dataset.position)).toEqual(["1", "2"]);
  });

  it("names the task, who queued it, and how long it has waited", () => {
    renderBoard(
      <SlotBoard body={waitingBody([queued()])} queue={[]} projectId="alpha" />,
    );

    const card = screen.getByTestId("queued-dispatch");
    expect(within(card).getByText("Waiting for a slot")).toBeVisible();
    expect(within(card).getByText(/Jeff Posey/)).toBeVisible();
    expect(within(card).getByText(/waiting 2m/)).toBeVisible();
    expect(within(card).getByRole("link")).toHaveAttribute("href", "/p/alpha/tasks/task-050");
  });

  it("says when one is being put through the gates rather than still waiting", () => {
    renderBoard(
      <SlotBoard
        body={waitingBody([queued({ status: "starting" })])}
        queue={[]}
        projectId="alpha"
      />,
    );

    expect(screen.getByText(/starting/)).toBeVisible();
  });

  it("renders the page's cancel control against the entry it belongs to", () => {
    const cancelled: string[] = [];
    renderBoard(
      <SlotBoard
        body={waitingBody([queued({ queue_id: "q_1" }), queued({ queue_id: "q_2", position: 2 })])}
        queue={[]}
        projectId="alpha"
        renderQueuedAction={(entry) => (
          <button type="button" onClick={() => cancelled.push(entry.queue_id)}>
            Cancel {entry.queue_id}
          </button>
        )}
      />,
    );

    screen.getByRole("button", { name: "Cancel q_2" }).click();

    expect(cancelled).toEqual(["q_2"]);
  });

  it("keeps the rail under an alarm and withholds only the cancel control", () => {
    // Same rule as the cells: work the machine has already been told to do is status,
    // and `statusOnly` suppresses actions rather than information (task-081).
    renderBoard(
      <SlotBoard
        body={waitingBody([queued()])}
        queue={[]}
        projectId="alpha"
        statusOnly
        renderQueuedAction={() => <button type="button">Cancel</button>}
      />,
    );

    expect(screen.getByTestId("queued-dispatch")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Cancel" })).not.toBeInTheDocument();
  });

  it("draws nothing at all when nothing is waiting", () => {
    renderBoard(<SlotBoard body={body({ queued: [] })} queue={[]} projectId="alpha" />);

    expect(screen.queryByTestId("slot-board-queue")).not.toBeInTheDocument();
  });

  it("shows why an entry is still waiting only when a start has been tried", () => {
    renderBoard(
      <SlotBoard
        body={waitingBody([queued({ detail: "task_on_hold: released from the review panel" })])}
        queue={[]}
        projectId="alpha"
      />,
    );

    expect(screen.getByTestId("slot-board-queue-detail")).toHaveTextContent("task_on_hold");
  });

  it("draws the board for a queue even on a machine with nothing else on it", () => {
    // The board returns null when it has no cells and nothing else to say. A queue is
    // something to say: a machine whose ceiling is unconfigured and whose only news is
    // that two dispatches are waiting must not render blank.
    renderBoard(
      <SlotBoard
        body={body({ dispatch_configured: false, occupied: 0, queued: [queued()] })}
        queue={[]}
        projectId="alpha"
      />,
    );

    expect(screen.getByTestId("slot-board-queue")).toBeVisible();
  });
});

describe("a run whose task closed while its session stayed open (task-482)", () => {
  const finished = (overrides: Partial<LiveRunView> = {}) =>
    run({
      run_id: "run_done",
      health: "work_done",
      holds_slot: false,
      task_id: "task-done",
      task_title: "Merged an hour ago",
      task_url: "/p/alpha/tasks/task-done",
      ...overrides,
    });

  it("takes no cell, because the server stopped counting it", () => {
    // The reported symptom: a task reading Completed beside a run holding a third of
    // the machine. The board follows `holds_slot` rather than the mode, so a run the
    // server has released cannot be drawn in a cell the server says is free.
    renderBoard(
      <SlotBoard
        body={body({ occupied: 0, runs: [finished()] })}
        queue={[task("task-one"), task("task-two"), task("task-three")]}
        projectId="alpha"
        renderQueueAction={(item) => <button type="button">Dispatch {item.id}</button>}
      />,
    );

    expect(cellStates()).toEqual(["run", "queued", "queued", "queued"]);
    expect(screen.getAllByRole("button", { name: /^Dispatch/ })).toHaveLength(3);
    expect(screen.getByTestId("slot-board-capacity")).toHaveTextContent("0 of 3 slots busy");
  });

  it("is still drawn, and says the work is done rather than that it is working", () => {
    renderBoard(
      <SlotBoard
        body={body({ occupied: 0, runs: [finished()] })}
        queue={[]}
        projectId="alpha"
        renderQueueAction={() => null}
      />,
    );

    expect(screen.getByText("Work done")).toHaveAttribute("data-health", "work_done");
  });
});

describe("a walk run, which started no agent (task-458)", () => {
  const walk = (overrides: Partial<LiveRunView> = {}) =>
    run({
      run_id: "run_walk",
      mode: "walk",
      session: false,
      task_id: "task-epic",
      task_title: "An epic whose children the server is flying",
      task_url: "/p/alpha/tasks/task-epic",
      ...overrides,
    });

  it("takes no cell from the board", () => {
    // It is in `runs` and not in `occupied`, like an interactive session and for a
    // sharper reason: there is no process. A walk run is normally over before any board
    // is drawn, and one that is not must not be shown taking a slot it does not take.
    renderBoard(
      <SlotBoard
        body={body({ runs: [walk()] })}
        queue={[task("task-one"), task("task-two"), task("task-three")]}
        projectId="alpha"
        renderQueueAction={(item) => <button type="button">Dispatch {item.id}</button>}
      />,
    );

    expect(cellStates()).toEqual(["run", "queued", "queued", "queued"]);
    expect(screen.getAllByRole("button", { name: /^Dispatch/ })).toHaveLength(3);
    expect(screen.getByTestId("slot-board-capacity")).toHaveTextContent("0 of 3 slots busy");
  });

  it("does not displace a dispatched run from a slot cell", () => {
    const layout = boardLayout(
      body({ occupied: 1, runs: [walk({ started_at: "2026-09-04T00:00:00Z" }), run()] }),
      [task("task-next")],
      "alpha",
    );

    expect(layout.cells.map((cell) => (cell.kind === "run" ? cell.run.run_id : cell.kind))).toEqual(
      ["run_a", "run_walk", "queued", "empty"],
    );
  });
});

function armed(overrides: Partial<ArmedProjectView> = {}): ArmedProjectView {
  return {
    project_id: "alpha",
    project_name: "Alpha Project",
    arming_id: "arm_aaaa",
    armed_by: "Jeff Posey",
    armed_at: "2026-09-04T00:30:00Z",
    bound: "1 of 3 starts used",
    starts_left: 2,
    posture: null,
    next_task_id: "task-077",
    next_task_title: "The one it would start",
    next_task_url: "/p/alpha/tasks/task-077",
    ...overrides,
  };
}

describe("the projects the pull mode is armed for (task-462)", () => {
  it("names who armed it and what is left of the bound", () => {
    // ac-5. Both facts, in the words the server composed, so the board cannot come to
    // disagree with the CLI about what "1 of 3" means.
    renderBoard(<SlotBoard body={body({ armed: [armed()] })} queue={[]} projectId="alpha" />);

    const row = screen.getByTestId("armed-project");
    expect(within(row).getByTestId("armed-bound")).toHaveTextContent("armed by Jeff Posey");
    expect(within(row).getByTestId("armed-bound")).toHaveTextContent("1 of 3 starts used");
  });

  it("says what it would start next, as a link to that task", () => {
    // The affordance the whole mode rests on: steering is reordering the queue, and a
    // person can only reorder in time if they can see the choice before it happens.
    renderBoard(<SlotBoard body={body({ armed: [armed()] })} queue={[]} projectId="alpha" />);

    const next = screen.getByTestId("armed-next");
    expect(next).toHaveTextContent("task-077");
    expect(next).toHaveTextContent("The one it would start");
    expect(next).toHaveAttribute("href", "/p/alpha/tasks/task-077");
  });

  it("says so rather than going quiet when that backlog has nothing claimable", () => {
    renderBoard(
      <SlotBoard
        body={body({ armed: [armed({ next_task_id: "", next_task_title: "" })] })}
        queue={[]}
        projectId="alpha"
      />,
    );

    expect(screen.getByTestId("armed-next-empty")).toBeVisible();
  });

  it("offers the Disarm the page supplies", () => {
    renderBoard(
      <SlotBoard
        body={body({ armed: [armed()] })}
        queue={[]}
        projectId="alpha"
        renderArmedAction={(entry) => <button type="button">Disarm {entry.project_id}</button>}
      />,
    );

    expect(screen.getByRole("button", { name: "Disarm alpha" })).toBeVisible();
  });

  it("withholds the Disarm under an alarm and still draws the rail", () => {
    // Same rule the waiting rail follows: work this machine will keep doing on its own
    // is status, and status is drawn on a bad day too. Only the action is withheld.
    renderBoard(
      <SlotBoard
        body={body({ armed: [armed()] })}
        queue={[]}
        projectId="alpha"
        statusOnly
        renderArmedAction={() => <button type="button">Disarm</button>}
      />,
    );

    expect(screen.getByTestId("slot-board-armed")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Disarm" })).not.toBeInTheDocument();
  });

  it("draws nothing at all when no project is armed", () => {
    renderBoard(<SlotBoard body={body({ armed: [] })} queue={[]} projectId="alpha" />);

    expect(screen.queryByTestId("slot-board-armed")).not.toBeInTheDocument();
  });

  it("states that a dispatch asked for by name starts first", () => {
    // The precedence rule, on the board rather than only in the design doc: somebody
    // watching both rails compete for one slot should be able to read which wins.
    renderBoard(
      <SlotBoard
        body={body({ armed: [armed()], queued: [queued()] })}
        queue={[]}
        projectId="alpha"
      />,
    );

    expect(screen.getByTestId("slot-board-armed")).toHaveTextContent(
      "A dispatch you asked for by name starts before any of these.",
    );
  });

  it("draws the board for an arming even on a machine with nothing else on it", () => {
    renderBoard(
      <SlotBoard
        body={body({ dispatch_configured: false, occupied: 0, armed: [armed()] })}
        queue={[]}
        projectId="alpha"
      />,
    );

    expect(screen.getByTestId("slot-board-armed")).toBeVisible();
  });

  it("names the armed project's own id on its row, not the page's", () => {
    // The rail is machine-wide, so a disarm wired to the page's project would disarm
    // the wrong one -- the same mistake the queue rail's cancel had to avoid.
    renderBoard(
      <SlotBoard
        body={body({ armed: [armed({ project_id: "beta", project_name: "Beta" })] })}
        queue={[]}
        projectId="alpha"
      />,
    );

    expect(screen.getByTestId("armed-project").dataset.projectId).toBe("beta");
  });
});

function pause(overrides: Partial<StartPauseView> = {}): StartPauseView {
  return {
    incident_id: "inc_7ffcc0210a984e39",
    kind: "usage_limit",
    kind_word: "usage limit",
    runner: "claude-opus-5",
    resets_at: "2026-09-18T23:40:00Z",
    opened_at: "2026-09-18T21:58:00Z",
    resumes_by_itself: true,
    queued: 1,
    projects: ["alpha"],
    detail: "paused until 2026-09-18T23:40:00+00:00: usage limit on claude-opus-5",
    ...overrides,
  };
}

/**
 * The pause the two clickless starters take on a usage limit (task-463).
 *
 * The assertions are on the *rendered sentence* rather than on the presence of the
 * notice, because the failure this rail exists to prevent is a person looking at a board
 * full of waiting work and concluding the machine is broken. A notice that appeared but
 * said "paused until 2026-09-18T23:40:00+00:00" would be that failure with extra steps:
 * the reset is a wall-clock fact to whoever is waiting for it, and ENGINEERING.md's rule
 * is to assert the value a browser will show.
 */
describe("the pause on a usage-limit incident (task-463)", () => {
  const heldQueue = (overrides: Partial<StartPauseView> = {}) =>
    body({
      occupied: 0,
      queued: [queued({ paused_by: "inc_7ffcc0210a984e39" })],
      queue_limit: 20,
      paused: [pause(overrides)],
    });

  it("says when the machine starts again, in the reader's own zone", () => {
    renderBoard(<SlotBoard body={heldQueue()} queue={[]} projectId="alpha" />);

    const notice = screen.getByTestId("slot-board-paused-queue");
    const local = new Date("2026-09-18T23:40:00Z").toLocaleTimeString([], {
      hour: "numeric",
      minute: "2-digit",
    });
    expect(notice).toHaveTextContent(`Paused until ${local}`);
    expect(notice).not.toHaveTextContent("23:40:00Z");
  });

  it("names the credential that ran out, so two runners are told apart", () => {
    renderBoard(<SlotBoard body={heldQueue()} queue={[]} projectId="alpha" />);

    expect(screen.getByTestId("slot-board-paused-queue")).toHaveTextContent(
      "usage limit on claude-opus-5",
    );
  });

  it("says nobody is needed, which is the whole reason it is drawn", () => {
    renderBoard(<SlotBoard body={heldQueue()} queue={[]} projectId="alpha" />);

    expect(screen.getByTestId("slot-board-paused-queue")).toHaveTextContent(
      "it starts again by itself",
    );
  });

  it("promises no time for a kind that needs a person", () => {
    // A dead login and a spend limit have no reset. A board counting down to one would
    // be undertaking something nothing on this machine has undertaken.
    renderBoard(
      <SlotBoard
        body={heldQueue({ kind: "auth", kind_word: "login", resets_at: null, resumes_by_itself: false })}
        queue={[]}
        projectId="alpha"
      />,
    );

    const notice = screen.getByTestId("slot-board-paused-queue");
    expect(notice).toHaveTextContent("Paused until the incident clears: login");
  });

  it("marks the held entry so a partly-paused queue is not misread", () => {
    renderBoard(
      <SlotBoard
        body={body({
          occupied: 0,
          queued: [
            queued({ queue_id: "q_1", paused_by: "inc_7ffcc0210a984e39" }),
            queued({ queue_id: "q_2", position: 2, task_id: "task-051" }),
          ],
          paused: [pause()],
        })}
        queue={[]}
        projectId="alpha"
      />,
    );

    const cards = screen.getAllByTestId("queued-dispatch");
    expect(cards.map((card) => card.dataset.pausedBy)).toEqual([
      "inc_7ffcc0210a984e39",
      undefined,
    ]);
    expect(cards[0]).toHaveTextContent("paused");
    expect(cards[1]).not.toHaveTextContent("paused");
  });

  it("draws the notice on the armed rail too, and says the bound is untouched", () => {
    renderBoard(
      <SlotBoard
        body={body({
          armed: [armed({ paused_by: "inc_7ffcc0210a984e39" })],
          paused: [pause({ queued: 0 })],
        })}
        queue={[]}
        projectId="alpha"
      />,
    );

    expect(screen.getByTestId("slot-board-paused-armed")).toHaveTextContent("usage limit");
    // The bound sits beside this badge, and a reader would otherwise assume a paused
    // arming was spending it. It is not: that is the difference from the disarm.
    expect(screen.getByTestId("armed-paused")).toHaveTextContent("bound untouched");
  });

  it("still names what the arming will start when the incident clears", () => {
    // The steering affordance survives the pause: a person who disagrees with the choice
    // has until the reset to go and move something, which is longer than they usually get.
    renderBoard(
      <SlotBoard
        body={body({
          armed: [armed({ paused_by: "inc_7ffcc0210a984e39" })],
          paused: [pause({ queued: 0 })],
        })}
        queue={[]}
        projectId="alpha"
      />,
    );

    expect(screen.getByTestId("armed-next")).toHaveTextContent("task-077");
  });

  it("draws no notice on a rail nothing on it is waiting for", () => {
    // A queue held by a Claude incident must not put a notice over an armed project
    // running on a credential that still answers.
    renderBoard(
      <SlotBoard
        body={body({
          occupied: 0,
          queued: [queued({ paused_by: "inc_7ffcc0210a984e39" })],
          armed: [armed()],
          paused: [pause()],
        })}
        queue={[]}
        projectId="alpha"
      />,
    );

    expect(screen.getByTestId("slot-board-paused-queue")).toBeVisible();
    expect(screen.queryByTestId("slot-board-paused-armed")).not.toBeInTheDocument();
  });

  it("draws nothing at all on a machine with no incident", () => {
    renderBoard(
      <SlotBoard
        body={body({ occupied: 0, queued: [queued()], armed: [armed()] })}
        queue={[]}
        projectId="alpha"
      />,
    );

    expect(screen.queryByTestId("slot-board-paused-queue")).not.toBeInTheDocument();
    expect(screen.queryByTestId("slot-board-paused-armed")).not.toBeInTheDocument();
  });

  it("is status, so it survives an alarm holding the page", () => {
    // `statusOnly` withholds actions. A machine that will start work on its own again is
    // exactly the status a person needs on a bad day.
    renderBoard(
      <SlotBoard body={heldQueue()} queue={[]} projectId="alpha" statusOnly />,
    );

    expect(screen.getByTestId("slot-board-paused-queue")).toBeVisible();
  });
});

function walk(overrides: Partial<EpicWalkView> = {}): EpicWalkView {
  return {
    walk_id: "walk_aaaa",
    project_id: "alpha",
    project_name: "Alpha Project",
    parent_task_id: "task-437",
    parent_task_title: "Teach the epic to land",
    parent_task_url: "/p/alpha/tasks/task-437",
    started_at: "2026-09-22T09:00:00Z",
    children_total: 7,
    children_completed: 3,
    children_in_flight: 1,
    children_remaining: 3,
    in_flight_task_ids: ["task-440"],
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

/**
 * The walk rail (task-523).
 *
 * Every assertion here is on a value a reader would act on -- the counts as the
 * sentence they are printed as, and the word for whether the walk has stopped -- rather
 * than on the container being present. A section that exists and says `0 of 0` is the
 * failure this task was filed about, and a test for the container passes over it.
 */
describe("the epics this machine is walking (task-523)", () => {
  it("names the epic, its counts and that it is still walking", () => {
    // ac-1. The parent is a link a reader can follow, and the counts read as one
    // sentence rather than three numbers nobody can order.
    renderBoard(<SlotBoard body={body({ walks: [walk()] })} queue={[]} projectId="alpha" />);

    const row = screen.getByTestId("epic-walk");
    expect(within(row).getByTestId("epic-walk-parent")).toHaveTextContent(
      "task-437Teach the epic to land",
    );
    expect(within(row).getByTestId("epic-walk-parent")).toHaveAttribute(
      "href",
      "/p/alpha/tasks/task-437",
    );
    expect(within(row).getByTestId("epic-walk-counts")).toHaveTextContent(
      "3 of 7 children done \u00b7 1 in flight \u00b7 3 to come",
    );
    expect(within(row).getByTestId("epic-walk-badge")).toHaveTextContent("walking");
    expect(within(row).getByTestId("epic-walk-state")).toHaveTextContent(
      "starting each child as its dependencies close",
    );
  });

  it("says a grounded walk has stopped, and why", () => {
    // ac-1, and the whole reason the section exists: a grounded walk and a quiet one
    // are the same row otherwise, and a reader who cannot tell them apart reads a
    // stall as progress.
    renderBoard(
      <SlotBoard
        body={body({
          walks: [
            walk({
              grounded: true,
              grounded_reason: "child_exhausted_attempts",
              grounded_word: "a child used both of its attempts",
              children_in_flight: 0,
              children_remaining: 4,
            }),
          ],
        })}
        queue={[]}
        projectId="alpha"
      />,
    );

    const row = screen.getByTestId("epic-walk");
    expect(within(row).getByTestId("epic-walk-badge")).toHaveTextContent("grounded");
    expect(within(row).getByTestId("epic-walk-state")).toHaveTextContent(
      "grounded: a child used both of its attempts. Nothing more takes off until a person acts.",
    );
  });

  it("distinguishes a walk that is only waiting from one that has stopped for good", () => {
    // task-467's distinction, on the page. Both are grounded; only this one resumes
    // with nobody involved, and telling a reader to go and act on it would solicit the
    // one gesture that cannot help.
    renderBoard(
      <SlotBoard
        body={body({
          walks: [
            walk({
              grounded: true,
              grounded_reason: "child_needs_a_human",
              grounded_word: "a child needs a person",
              resumes_by_itself: true,
              waiting_on_task_id: "task-147",
              waiting_on_task_title: "Acceptance checks",
              waiting_on_task_url: "/p/alpha/tasks/task-147",
            }),
          ],
        })}
        queue={[]}
        projectId="alpha"
      />,
    );

    const row = screen.getByTestId("epic-walk");
    expect(within(row).getByTestId("epic-walk-badge")).toHaveTextContent("waiting");
    expect(within(row).getByTestId("epic-walk-state")).toHaveTextContent(
      "waiting on task-147: a child needs a person. It takes off again on its own when that clears.",
    );
    expect(within(row).getByTestId("epic-walk-waiting-on")).toHaveAttribute(
      "href",
      "/p/alpha/tasks/task-147",
    );
  });

  it("takes no slot cell and changes nothing the free cells offer", () => {
    // ac-2. A walk holds no slot (task-458), so the board is the board it would be
    // without one: the same cells, the same states, the same offer.
    const without = boardLayout(body(), [task("task-next")], "alpha");
    const with_ = boardLayout(body({ walks: [walk()] }), [task("task-next")], "alpha");

    expect(with_.cells.map((cell) => cell.kind)).toEqual(without.cells.map((cell) => cell.kind));

    renderBoard(
      <SlotBoard body={body({ walks: [walk()] })} queue={[task("task-next")]} projectId="alpha" />,
    );

    expect(cellStates()).toEqual(["queued", "empty", "empty"]);
    expect(screen.getByTestId("slot-board-capacity")).toHaveTextContent(
      capacitySentence(body({ walks: [walk()] })),
    );
  });

  it("draws nothing at all when no epic is being walked", () => {
    // ac-3. An "Epic walks (0)" header on every calm day is the page's loudest element
    // saying nothing.
    renderBoard(<SlotBoard body={body({ walks: [] })} queue={[]} projectId="alpha" />);

    expect(screen.queryByTestId("slot-board-walks")).not.toBeInTheDocument();
    expect(screen.queryByText(/Epics being walked/)).not.toBeInTheDocument();
  });

  it("is status, so it survives an alarm holding the page", () => {
    // It offers no action, so there is nothing for `statusOnly` to withhold -- and a
    // machine dispatching children on its own is exactly the status a bad day needs.
    renderBoard(
      <SlotBoard body={body({ walks: [walk()] })} queue={[]} projectId="alpha" statusOnly />,
    );

    expect(screen.getByTestId("epic-walk-counts")).toBeVisible();
  });

  it("says so rather than printing zeroes when the backlog cannot be read", () => {
    // A project unregistered while its walk is still open. "0 of 0 children done" would
    // read as a finished epic, which is the one thing it certainly is not.
    expect(walkCountsSentence(walk({ children_total: 0 }))).toBe(
      "children could not be read from this project's backlog",
    );
  });

  it("falls back to the raw stop reason when the server has a word this build lacks", () => {
    expect(walkState(walk({ grounded: true, grounded_reason: "some_new_stop" })).sentence).toBe(
      "grounded: some_new_stop. Nothing more takes off until a person acts.",
    );
  });

  it("draws the board for a walk on a machine with nothing else on it", () => {
    renderBoard(
      <SlotBoard
        body={body({ dispatch_configured: false, occupied: 0, walks: [walk()] })}
        queue={[]}
        projectId="alpha"
      />,
    );

    expect(screen.getByTestId("slot-board-walks")).toBeVisible();
  });
});
