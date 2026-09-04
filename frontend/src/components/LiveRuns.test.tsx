import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import type { LiveRunView, LiveRunsView, MachineHolderView } from "../api/types";
import {
  BUSY_POLL_MS,
  IDLE_POLL_MS,
  LiveRunCount,
  LiveRunsPage,
  MachineCapacityRow,
  capacitySentence,
  liveRunsPollInterval,
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

  it("reads zero before the first answer arrives", () => {
    renderIn(<LiveRunCount body={null} />);
    expect(screen.getByTestId("live-run-count")).toHaveTextContent("0");
  });
});

describe("the Dashboard capacity row", () => {
  it("states capacity and names what is running", () => {
    renderIn(<MachineCapacityRow projectId="alpha" body={body({ occupied: 1, runs: [run()] })} />);
    const row = screen.getByTestId("machine-capacity");
    expect(row).toHaveTextContent("1 of 3 slots busy");
    expect(row).toHaveTextContent("Teach the queue to count");
  });

  it("caps the names it lists and says how many it left out", () => {
    const runs = [
      run({ run_id: "run_a", task_title: "First" }),
      run({ run_id: "run_b", task_title: "Second" }),
      run({ run_id: "run_c", task_title: "Third" }),
    ];
    renderIn(<MachineCapacityRow projectId="alpha" body={body({ occupied: 3, runs })} />);
    const row = screen.getByTestId("machine-capacity");
    expect(row).toHaveTextContent("First, Second +1 more");
    expect(row).not.toHaveTextContent("Third");
  });

  it("still renders when the machine is idle", () => {
    // A row that disappeared could not be told from one that had broken, and "nothing
    // is running" is what a person opening the Dashboard most often wants to read.
    renderIn(<MachineCapacityRow projectId="alpha" body={body()} />);
    expect(screen.getByTestId("machine-capacity")).toHaveTextContent("nothing running");
  });

  it("renders nothing at all before the first answer", () => {
    const { container } = renderIn(<MachineCapacityRow projectId="alpha" body={null} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("links to the Runs tab of the project it is on", () => {
    renderIn(<MachineCapacityRow projectId="alpha" body={body()} />);
    expect(screen.getByTestId("machine-capacity")).toHaveAttribute("href", "/p/alpha/runs");
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

  it("says a batch run whose process is gone is not working", () => {
    renderIn(<LiveRunsPage body={body({ occupied: 1, runs: [run({ health: "orphaned" })] })} />);
    expect(screen.getByText("Process gone")).toHaveAttribute("data-health", "orphaned");
  });

  it("shows an empty machine as empty rather than as a blank table", () => {
    renderIn(<LiveRunsPage body={body()} />);
    expect(screen.getByTestId("no-live-runs")).toBeInTheDocument();
  });

  it("lists a merge in progress separately from the run slots", () => {
    renderIn(<LiveRunsPage body={body({ holders: [holder()] })} />);
    const section = screen.getByRole("heading", { name: "Also on this machine" }).closest("section");
    expect(section).not.toBeNull();
    expect(within(section as HTMLElement).getByText(/Finishing task-002/)).toBeInTheDocument();
    // Not counted as an occupied slot: it holds a lock, not a run slot.
    expect(screen.getByTestId("capacity-sentence")).toHaveTextContent("0 of 3 slots busy");
  });

  it("names the repository a merge runway is blocking", () => {
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
