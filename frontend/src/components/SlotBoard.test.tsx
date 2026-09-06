import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import type { LiveRunView, LiveRunsView, MachineHolderView, TaskRead } from "../api/types";
import { BOARD_CELL_LIMIT, SlotBoard, boardLayout, orderedRuns } from "./SlotBoard";

/**
 * The slot board (task-092), rendered from a response object.
 *
 * The layout rules are a pure function and are tested as one: how many cells there are,
 * which are busy, and what fills the rest are the decisions worth pinning, and asserting
 * them through the DOM would only add a rendering to every failure message. What the
 * DOM tests cover is what a person's browser actually acts on -- the health word, the
 * elapsed time, the link a foreign run points at, and the button on a free cell.
 */

function run(overrides: Partial<LiveRunView> = {}): LiveRunView {
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

function task(id: string, overrides: Partial<TaskRead> = {}): TaskRead {
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
    spec: { summary: `Summary of ${id}`, description: "Body." },
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
