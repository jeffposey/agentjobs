import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import type {
  EpicWalkView,
  LiveRunView,
  LiveRunsView,
  MachineHolderView,
} from "../api/types";
import {
  BUSY_POLL_MS,
  HealthBadge,
  IDLE_POLL_MS,
  LiveRunCount,
  LiveRunsPage,
  capacitySentence,
  liveFinishes,
  liveRunsPollInterval,
  runningCount,
  unexplainedRunways,
} from "./LiveRuns";

/**
 * The machine-wide surfaces (task-328), rendered from a response object.
 *
 * Every component here is prop-driven for that reason: the page, the Dashboard row and
 * the badge take the same body, so one fixture exercises all three and none of them
 * needs a query client or a server. The polling *decision* is a pure function tested
 * directly, because a test that waited two seconds for a refetch would be measuring
 * react-query rather than this file.
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

function walk(overrides: Partial<EpicWalkView> = {}): EpicWalkView {
  return {
    walk_id: "walk_aaaa",
    project_id: "alpha",
    project_name: "Alpha Project",
    parent_task_id: "task-437",
    parent_task_title: "Teach the epic to land",
    parent_task_url: "/p/alpha/tasks/task-437",
    started_at: "2026-09-22T09:00:00Z",
    children_total: 3,
    children_completed: 1,
    children_in_flight: 1,
    children_remaining: 1,
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

function renderIn(node: React.ReactNode) {
  return render(<MemoryRouter initialEntries={["/p/alpha"]}>{node}</MemoryRouter>);
}

describe("the poll interval", () => {
  it("polls fast while something is running", () => {
    expect(liveRunsPollInterval(body({ runs: [run()], occupied: 1 }))).toBe(BUSY_POLL_MS);
  });

  it("polls fast while a merge holds the machine, even with no runs", () => {
    expect(liveRunsPollInterval(body({ holders: [holder()] }))).toBe(BUSY_POLL_MS);
  });

  it("polls fast while an epic walk is still taking off, with no run of its own", () => {
    // task-523. A walk starts children with nobody clicking anything, so the board has
    // to be watching when the slot fills.
    expect(
      liveRunsPollInterval(
        body({ walks: [{ ...walk(), grounded: false }] }),
      ),
    ).toBe(BUSY_POLL_MS);
  });

  it("does not treat a grounded walk as a busy machine", () => {
    // One waiting on a review can wait for days. Polling every two seconds for it would
    // pay a busy machine's cost for an idle one.
    expect(
      liveRunsPollInterval(
        body({ walks: [{ ...walk(), grounded: true, resumes_by_itself: true }] }),
      ),
    ).toBe(IDLE_POLL_MS);
  });

  it("keeps polling when nothing is running, rather than stopping", () => {
    // The per-task panel stops, because the only thing that starts a run there is the
    // button beside it. Nothing guarantees that here: the next run will be started from
    // another project, from the CLI, or by an epic walk, and a badge that had stopped
    // polling would sit at zero through all of it.
    expect(liveRunsPollInterval(body())).toBe(IDLE_POLL_MS);
    expect(liveRunsPollInterval(body())).not.toBe(false);
  });
});

describe("the capacity sentence", () => {
  it("says how many of the machine's slots are busy", () => {
    expect(capacitySentence(body({ occupied: 2 }))).toBe("2 of 3 slots busy");
  });

  it("agrees in number with a one-slot machine", () => {
    expect(capacitySentence(body({ occupied: 1, max_concurrent_runs: 1 }))).toBe(
      "1 of 1 slot busy",
    );
  });

  it("does not quote a ceiling nobody configured", () => {
    expect(capacitySentence(body({ dispatch_configured: false }))).toBe(
      "Dispatch is not configured on this machine",
    );
  });

  it("says a merge is in progress, without counting it as a slot", () => {
    // task-352: "0 of 3 slots busy" on its own, beside a five-minute gate, read as
    // nothing happening. The slot count is still the guard's number and still zero.
    expect(capacitySentence(body({ holders: [holder()] }))).toBe("0 of 3 slots busy · 1 merging");
    expect(
      capacitySentence(body({ holders: [holder(), holder({ lock_name: "task-003" })] })),
    ).toBe("0 of 3 slots busy · 2 merging");
  });

  it("does not count the runway as a second merge", () => {
    const holders = [holder(), holder({ kind: "runway", lock_name: "runway-abc", task_id: "" })];
    expect(capacitySentence(body({ holders }))).toBe("0 of 3 slots busy · 1 merging");
  });
});

describe("what counts as running", () => {
  it("counts dispatched runs and finishes, and nothing else", () => {
    const holders = [holder(), holder({ kind: "runway", lock_name: "runway-abc", task_id: "" })];
    expect(runningCount(body({ runs: [run()], occupied: 1, holders }))).toBe(2);
    expect(runningCount(null)).toBe(0);
    expect(liveFinishes(body({ holders }))).toHaveLength(1);
  });

  it("explains a runway by the finish holding it", () => {
    // The runway is the same finish's lock on the repository, so it is not a second
    // thing happening -- unless no listed finish holds it, when it is still worth a line.
    const held = holder({ kind: "runway", lock_name: "runway-abc", task_id: "", finish_id: "fin_abc" });
    expect(unexplainedRunways(body({ holders: [holder(), held] }))).toEqual([]);
    expect(unexplainedRunways(body({ holders: [held] }))).toEqual([held]);
    const other = holder({ kind: "runway", lock_name: "runway-def", task_id: "", finish_id: "fin_zzz" });
    expect(unexplainedRunways(body({ holders: [holder(), other] }))).toEqual([other]);
  });
});

describe("the nav badge", () => {
  it("shows the live count", () => {
    renderIn(<LiveRunCount body={body({ runs: [run(), run({ run_id: "run_b" })] })} />);
    expect(screen.getByTestId("live-run-count")).toHaveTextContent("2");
  });

  it("reads zero when nothing is running, rather than holding a stale number", () => {
    renderIn(<LiveRunCount body={body()} />);
    expect(screen.getByTestId("live-run-count")).toHaveTextContent("0");
  });

  it("counts a finish in progress as running", () => {
    // task-352: the badge read 0 through the whole of task-092's gate.
    renderIn(<LiveRunCount body={body({ holders: [holder()] })} />);
    expect(screen.getByTestId("live-run-count")).toHaveTextContent("1");
  });

  it("reads zero before the first answer arrives", () => {
    renderIn(<LiveRunCount body={null} />);
    expect(screen.getByTestId("live-run-count")).toHaveTextContent("0");
  });
});

describe("the Runs tab", () => {
  it("links each row into the project that owns it, not the one being viewed", () => {
    const runs = [
      run(),
      run({
        run_id: "run_b",
        task_id: "task-500",
        task_title: "Rewrite the importer",
        project_id: "beta",
        project_name: "Beta Project",
        // The server built this, which is the point of the assertion below.
        task_url: "/p/beta/tasks/task-500",
      }),
    ];
    renderIn(<LiveRunsPage body={body({ occupied: 2, runs })} />);

    expect(screen.getByRole("link", { name: /task-001/ })).toHaveAttribute(
      "href",
      "/p/alpha/tasks/task-001",
    );
    expect(screen.getByRole("link", { name: /task-500/ })).toHaveAttribute(
      "href",
      "/p/beta/tasks/task-500",
    );
    expect(screen.getByText("Beta Project")).toBeInTheDocument();
  });

  it("says a parked run is waiting on a human, not that it is working", () => {
    renderIn(<LiveRunsPage body={body({ occupied: 1, runs: [run({ health: "parked" })] })} />);
    const row = screen.getByText("Waiting on you");
    expect(row).toHaveAttribute("data-health", "parked");
    expect(screen.queryByText("Working")).toBeNull();
  });

  it("says the work is done for a run whose task closed (task-482)", () => {
    // The pair that made a third of the machine unavailable: a task reading Completed
    // beside a run reading `running`. The run is real and the session is open, so it is
    // still listed -- with the word that says why it is not in the occupied count.
    renderIn(
      <LiveRunsPage
        body={body({ occupied: 0, runs: [run({ health: "work_done", holds_slot: false })] })}
      />,
    );
    expect(screen.getByText("Work done")).toHaveAttribute("data-health", "work_done");
    expect(screen.queryByText("Working")).toBeNull();
  });

  it("says feedback is waiting rather than that the run is working (task-384)", () => {
    // The pair that made a person wait fifty minutes: a task reading "Revising" beside a
    // live run. Both "working" and "feedback waiting" were true; only one of them answers
    // the question the reader has, so `run_health` renders this one and this asserts it
    // reaches the badge rather than falling through to the raw value.
    renderIn(<LiveRunsPage body={body({ occupied: 1, runs: [run({ health: "handback" })] })} />);
    // "Feedback", in the working blue: it continues with nobody acting (owner, 2026-09-24).
    expect(screen.getByText("Feedback")).toHaveAttribute("data-health", "handback");
    expect(screen.getByText("Feedback")).toHaveAttribute("data-status-category", "working");
    expect(screen.queryByText("Working")).toBeNull();
  });

  it("says a batch run whose process is gone is not working", () => {
    renderIn(<LiveRunsPage body={body({ occupied: 1, runs: [run({ health: "orphaned" })] })} />);
    expect(screen.getByText("Process gone")).toHaveAttribute("data-health", "orphaned");
  });

  it("shows an empty machine as empty rather than as a blank table", () => {
    renderIn(<LiveRunsPage body={body()} />);
    expect(screen.getByTestId("no-live-runs")).toBeInTheDocument();
  });

  it("lists a finish in progress as a run, with its task, its step and its time", () => {
    // task-352: this exact body -- one finish, no runs -- rendered "Nothing is running
    // on this machine right now" with the finish in a footnote under it.
    renderIn(<LiveRunsPage body={body({ holders: [holder({ detail: "gate" })] })} />);

    expect(screen.queryByTestId("no-live-runs")).toBeNull();
    const row = screen.getByRole("link", { name: /task-002/ }).closest("tr");
    expect(row).not.toBeNull();
    expect(screen.getByRole("link", { name: /task-002/ })).toHaveAttribute(
      "href",
      "/p/alpha/tasks/task-002",
    );
    expect(within(row as HTMLElement).getByText("Finishing")).toHaveAttribute(
      "data-finish-step",
      "gate",
    );
    expect(within(row as HTMLElement).getByText("Running the gate")).toBeInTheDocument();
    expect(within(row as HTMLElement).getByText("30s")).toBeInTheDocument();
    // Still not an occupied slot: it holds a lock, not a run slot -- and the sentence
    // says which.
    expect(screen.getByTestId("capacity-sentence")).toHaveTextContent(
      "0 of 3 slots busy · 1 merging",
    );
    expect(screen.queryByRole("heading", { name: "Also on this machine" })).toBeNull();
  });

  it("does not badge a finish against an already-closed task as Finishing", () => {
    // task-514. The board renders from the lock, so it went on saying Finishing about a
    // task whose page said Completed. The lock is real and stays listed; the word is not.
    renderIn(
      <LiveRunsPage
        body={body({ holders: [holder({ detail: "overtaken", overtaken: true })] })}
      />,
    );

    const row = screen.getByRole("link", { name: /task-002/ }).closest("tr");
    expect(within(row as HTMLElement).queryByText("Finishing")).toBeNull();
    expect(within(row as HTMLElement).getByText("Overtaken")).toBeInTheDocument();
    expect(within(row as HTMLElement).getByText("Task already closed")).toBeInTheDocument();
    // ...and it is not counted as something merging, because it is not merging.
    expect(screen.getByTestId("capacity-sentence")).toHaveTextContent("0 of 3 slots busy");
    expect(screen.getByTestId("capacity-sentence")).not.toHaveTextContent("merging");
  });

  it("says a finish queued for the runway is queued, in words", () => {
    renderIn(<LiveRunsPage body={body({ holders: [holder({ detail: "runway" })] })} />);
    expect(screen.getByText("Queued for the merge runway")).toBeInTheDocument();
  });

  it("does not list the runway a listed finish is holding", () => {
    const holders = [
      holder(),
      holder({ kind: "runway", lock_name: "runway-abc", task_id: "", task_url: "" }),
    ];
    renderIn(<LiveRunsPage body={body({ holders })} />);
    expect(screen.queryByText(/Merge runway/)).toBeNull();
    expect(screen.getAllByRole("row")).toHaveLength(2); // the header and the finish
  });

  it("names the repository a merge runway is blocking when no finish explains it", () => {
    renderIn(
      <LiveRunsPage
        body={body({
          holders: [holder({ kind: "runway", lock_name: "runway-abc", task_id: "", task_url: "" })],
        })}
      />,
    );
    expect(screen.getByText(/Merge runway/)).toHaveTextContent("Alpha Project");
  });

  it("offers no way to cancel anything", () => {
    // Read-only is a decision, not an omission (task-328 out_of_scope, task-312).
    renderIn(<LiveRunsPage body={body({ occupied: 1, runs: [run()] })} />);
    expect(screen.queryByRole("button", { name: /cancel/i })).toBeNull();
  });
});

describe("a run's badge in the task status colours (task-562)", () => {
  it.each([
    ["working", "Working", "working"],
    ["starting", "Starting", "queued"],
    ["finishing", "Finishing", "finishing"],
    ["parked", "Waiting on you", "needs_you"],
    ["handback", "Feedback", "working"],
  ])("draws %s as the task status it names", (health, label, category) => {
    render(<HealthBadge health={health} />);

    const badge = screen.getByText(label);
    expect(badge).toHaveAttribute("data-health", health);
    expect(badge).toHaveAttribute("data-status-category", category);
    expect(badge.style.backgroundColor).not.toBe("");
  });

  it("leaves a process-only state its own colour", () => {
    render(<HealthBadge health="silent" />);

    expect(screen.getByText("No output")).toHaveClass("bg-orange-900");
    expect(screen.getByText("No output")).not.toHaveAttribute("data-status-category");
  });
});

describe("a run's badge moves only while the run is doing something (task-570)", () => {
  it.each([
    ["working", "Working", "orbit"],
    ["starting", "Starting", "ignite"],
    ["finishing", "Finishing", "sweep"],
  ])("moves %s", (health, label, motion) => {
    render(<HealthBadge health={health} />);

    expect(screen.getByText(label)).toHaveAttribute("data-motion", motion);
    expect(screen.getByText(label)).toHaveClass(`chip-motion-${motion}`);
  });

  it.each([
    ["handback", "Feedback"],
    ["parked", "Waiting on you"],
    ["silent", "No output"],
    ["idle", "Idle"],
  ])("keeps %s still", (health, label) => {
    render(<HealthBadge health={health} />);

    expect(screen.getByText(label)).not.toHaveAttribute("data-motion");
    expect(screen.getByText(label).className).not.toContain("chip-motion");
  });
});
