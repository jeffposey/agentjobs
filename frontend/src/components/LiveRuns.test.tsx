import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type {
  EpicWalkView,
  LiveRunView,
  LiveRunsView,
  MachineHolderView,
} from "../api/types";
import {
  BUSY_POLL_MS,
  FinishBadge,
  HealthBadge,
  IDLE_POLL_MS,
  capacitySentence,
  finishDetail,
  landingCount,
  liveFinishes,
  liveRunsPollInterval,
  runCounts,
  unexplainedRunways,
} from "./LiveRuns";

/**
 * The machine-wide surfaces (task-328), rendered from a response object.
 *
 * Every component here is prop-driven for that reason: the slot board and the header's
 * readout take the same body, so one fixture exercises both and neither needs a query
 * client or a server. The polling *decision* is a pure function tested
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
    expect(runCounts(body({ runs: [run()], occupied: 1, holders }))).toEqual({
      working: 1,
      landing: 1,
    });
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

describe("the working and landing counts", () => {
  // What the header's blue and violet dots show (task-608; task-588's one green dot
  // before it, the Runs tab's badge before that). The rendering is NavStatus.test.tsx's;
  // the counts are this file's.
  it("counts every run that is not landing as working", () => {
    expect(runCounts(body({ runs: [run(), run({ run_id: "run_b" })] }))).toEqual({
      working: 2,
      landing: 0,
    });
  });

  it("is zero when nothing is running", () => {
    expect(runCounts(body())).toEqual({ working: 0, landing: 0 });
  });

  it("counts a finish in progress as landing", () => {
    // task-352: the badge read 0 through the whole of task-092's gate.
    expect(runCounts(body({ holders: [holder()] }))).toEqual({ working: 0, landing: 1 });
  });

  it("counts working and landing runs disjointly", () => {
    // A run finishing itself (task-533) keeps its run row. Before task-608 the one count
    // was runs plus finishes, so it sat there as working; now it is landing only, and a
    // finish holder beside it is a second landing, never a working run.
    const live = body({
      runs: [
        run(),
        run({ run_id: "run_b", task_id: "task-003", health: "finishing" }),
        run({ run_id: "run_c", task_id: "task-004", health: "parked" }),
      ],
      holders: [holder(), holder({ kind: "runway", lock_name: "runway-abc", task_id: "" })],
    });
    expect(runCounts(live)).toEqual({ working: 2, landing: 2 });
    // The same classification the capacity sentence and the board use.
    expect(landingCount(live)).toBe(2);
    expect(capacitySentence(live)).toContain("2 merging");
  });

  it("moves a landing that fails back to working, without a double count or a drop", () => {
    // The same task between two polls: its run finishing itself, then the gate goes red
    // and the run is back at work.
    const landing = run({ run_id: "run_b", task_id: "task-003", health: "finishing" });
    const before = runCounts(body({ runs: [run(), landing] }));
    const after = runCounts(body({ runs: [run(), { ...landing, health: "working" }] }));
    expect(before).toEqual({ working: 1, landing: 1 });
    expect(after).toEqual({ working: 2, landing: 0 });
    expect(after.working + after.landing).toBe(before.working + before.landing);
  });

  it("counts an overtaken finish in neither", () => {
    // Its task is already closed (task-514): nothing is being worked and nothing lands.
    expect(runCounts(body({ holders: [holder({ overtaken: true })] }))).toEqual({
      working: 0,
      landing: 0,
    });
  });

  it("is zero before the first answer arrives", () => {
    expect(runCounts(null)).toEqual({ working: 0, landing: 0 });
  });
});

// The Runs tab rendered these words in its rows until task-588 retired it; the slot
// board still does, so they are asserted on the components rather than on a page.
describe("a run's state, in words", () => {
  it("says a parked run is waiting on a human, not that it is working", () => {
    render(<HealthBadge health="parked" />);
    expect(screen.getByText("Waiting on you")).toHaveAttribute("data-health", "parked");
    expect(screen.queryByText("Working")).toBeNull();
  });

  it("says what its closed task says for a run whose task closed (task-482, task-577)", () => {
    // The pair that made a third of the machine unavailable: a task reading Completed
    // beside a run reading `running`. The run is real and the session is open, so it is
    // still drawn -- in its task's own word and colour, not a word of its own.
    render(
      <>
        <HealthBadge
          health="work_done"
          task={{ task_display_status: "Completed", task_status_category: "closed" }}
        />
        <HealthBadge
          health="work_done"
          task={{ task_display_status: "Cancelled", task_status_category: "closed_unfinished" }}
        />
      </>,
    );
    expect(screen.getByText("Completed")).toHaveAttribute("data-status-category", "closed");
    expect(screen.getByText("Cancelled")).toHaveAttribute("data-status-category", "closed_unfinished");
    expect(screen.queryByText("Working")).toBeNull();
  });

  it("falls back to a plain word when a closed run's task cannot be read", () => {
    render(<HealthBadge health="work_done" />);
    expect(screen.getByText("Task closed")).toHaveAttribute("data-health", "work_done");
  });

  it("says feedback is waiting rather than that the run is working (task-384)", () => {
    render(<HealthBadge health="handback" />);
    // "Feedback", in the working blue: it continues with nobody acting (owner, 2026-09-24).
    expect(screen.getByText("Feedback")).toHaveAttribute("data-health", "handback");
    expect(screen.getByText("Feedback")).toHaveAttribute("data-status-category", "working");
  });

  it("says a batch run whose process is gone is not working", () => {
    render(<HealthBadge health="orphaned" />);
    expect(screen.getByText("Process gone")).toHaveAttribute("data-health", "orphaned");
  });

  it("draws a finish in progress as Landing, with its step in words", () => {
    render(<FinishBadge finish={holder({ detail: "gate" })} />);
    expect(screen.getByText("Landing")).toHaveAttribute("data-finish-step", "gate");
    expect(finishDetail("gate", undefined)).toBe("Running the gate");
    expect(finishDetail("runway", "task-009")).toBe("Queued for the merge runway, behind task-009");
  });

  it("does not badge a finish against an already-closed task as Landing", () => {
    // task-514. The board renders from the lock, so it went on saying Finishing about a
    // task whose page said Completed. The lock is real and stays drawn; the word is not.
    render(<FinishBadge finish={holder({ detail: "overtaken", overtaken: true })} />);
    expect(screen.queryByText("Landing")).toBeNull();
    expect(screen.getByText("Overtaken")).toBeInTheDocument();
    expect(finishDetail("overtaken", undefined)).toBe("Task already closed");
  });
});

describe("the capacity sentence, for a finish that merged nothing", () => {
  it("does not count an overtaken finish as merging (task-514)", () => {
    expect(
      capacitySentence(body({ holders: [holder({ detail: "overtaken", overtaken: true })] })),
    ).toBe("0 of 3 slots busy");
  });
});

describe("a run's badge in the task status colours (task-562)", () => {
  it.each([
    ["working", "Working", "working"],
    ["starting", "Starting", "queued"],
    ["finishing", "Landing", "finishing"],
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
    ["starting", "Starting", "orbit"],
    ["finishing", "Landing", "orbit"],
    ["parked", "Waiting on you", "flash"],
  ])("moves %s", (health, label, motion) => {
    render(<HealthBadge health={health} />);

    expect(screen.getByText(label)).toHaveAttribute("data-motion", motion);
    expect(screen.getByText(label)).toHaveClass(`chip-motion-${motion}`);
  });

  it.each([
    ["handback", "Feedback"],
    ["silent", "No output"],
    ["idle", "Idle"],
  ])("keeps %s still", (health, label) => {
    render(<HealthBadge health={health} />);

    expect(screen.getByText(label)).not.toHaveAttribute("data-motion");
    expect(screen.getByText(label).className).not.toContain("chip-motion");
  });
});
