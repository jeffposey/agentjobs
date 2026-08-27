import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { TaskFinishView } from "../api/types";
import {
  FINISH_POLL_MS,
  FinishPanel,
  finishBadge,
  finishDetail,
  finishHeadline,
  finishPollInterval,
  formatFinishElapsed,
  gateNote,
} from "./FinishPanel";

/**
 * The panel task-321 exists for: Jeff, on task-296, *"when I click approve and it auto
 * finishes, there is no feedback really"*.
 *
 * What these assert is the rendered value a reader acts on, never the presence of
 * markup -- so a state whose sentence went missing fails here rather than shipping as a
 * panel that says "Finish" and nothing else.
 */

function finish(overrides: Partial<TaskFinishView> = {}): TaskFinishView {
  return {
    task_id: "task-296",
    project_id: "agentjobs",
    state: "running",
    live: true,
    finish_id: "fin_070fa915",
    started_at: "2026-08-27T15:47:31+00:00",
    finished_at: "",
    elapsed_seconds: 42,
    branch: "fix/task-296-stall-detection",
    worktree: "C:/projects/worktrees/agentjobs-296",
    current_step: "gate",
    steps: [
      { name: "preflight", state: "done", detail: "fix/task-296 at f886154a", seconds: 1.4, meaning: "Checking the branch" },
      { name: "gate", state: "running", detail: "", seconds: 0, meaning: "Running the full gate" },
    ],
    gate: {
      stage: "pytest",
      stages_run: 6,
      stages_total: 10,
      running: true,
      passed: null,
      seconds: 0,
      failed_stage: "",
    },
    reason: "",
    stopped_at: "",
    merge_commit: "",
    output_source: "none",
    output_tail: "",
    output_url: "/api/projects/agentjobs/dispatch/finishes/task-296/output",
    ...overrides,
  };
}

describe("polling", () => {
  it("polls while a finish is live and stops when it ends", () => {
    expect(finishPollInterval(finish())).toBe(FINISH_POLL_MS);
    expect(finishPollInterval(finish({ live: false }))).toBe(false);
    // A task with no finish is the ordinary case, and it must not put the page on a
    // two-second clock forever.
    expect(finishPollInterval(null)).toBe(false);
  });
});

describe("the sentence a reader acts on", () => {
  it("names the merge rather than the outcome word", () => {
    const done = finish({
      state: "finished",
      live: false,
      merge_commit: "7733b2b08c8138bba3171047fa25c847cf370ad9",
    });
    expect(finishHeadline(done)).toContain("7733b2b0");
    expect(finishHeadline(done)).toContain("verified live");
  });

  it("says nothing was merged when nothing was", () => {
    const stopped = finish({ state: "escalated", live: false, stopped_at: "gate", reason: "gate_failed" });
    expect(finishHeadline(stopped)).toContain("nothing was merged");
    expect(finishDetail(stopped)).toContain("gate");
  });

  it("distinguishes an escalation that had already merged", () => {
    const stopped = finish({
      state: "escalated",
      live: false,
      stopped_at: "restart",
      merge_commit: "abcdef1234",
    });
    // The one thing a reader must not be told wrongly: whether main moved.
    expect(finishHeadline(stopped)).toContain("Merged as abcdef12");
  });

  it("explains a finish whose process is gone", () => {
    const dead = finish({ state: "interrupted", live: false });
    expect(finishDetail(dead)).toContain("process is gone");
  });
});

describe("the badge", () => {
  it("never calls a finish that stopped 'done'", () => {
    // The first word read on the card, so it is the one that has to be true. A red
    // gate merged nothing and "Done" beside that is the worst available summary.
    expect(finishBadge(finish({ state: "escalated", live: false }))).toBe("Stopped");
    expect(finishBadge(finish({ state: "interrupted", live: false }))).toBe("Interrupted");
    expect(finishBadge(finish({ state: "finished", live: false }))).toBe("Done");
    expect(finishBadge(finish({ state: "starting" }))).toBe("Starting");
    expect(finishBadge(finish())).toBe("Running");
  });
});

describe("the gate line", () => {
  it("says which stage and how far in", () => {
    expect(gateNote(finish())).toBe("Gate: pytest — 6 of 10");
  });

  it("reports a gate that never wrote per-stage records without inventing a counter", () => {
    const old = finish({
      gate: { stage: "", stages_run: 0, stages_total: 0, running: true, passed: null, seconds: 0, failed_stage: "" },
    });
    expect(gateNote(old)).toBe("Gate: running");
  });

  it("names the stage a red gate failed at", () => {
    const red = finish({
      state: "escalated",
      live: false,
      gate: { stage: "", stages_run: 7, stages_total: 10, running: false, passed: false, seconds: 96, failed_stage: "pytest" },
    });
    expect(gateNote(red)).toContain("red at pytest");
    expect(gateNote(red)).toContain("never merges");
  });
});

describe("elapsed", () => {
  it("reads as a person reads it", () => {
    expect(formatFinishElapsed(42)).toBe("42s");
    expect(formatFinishElapsed(203)).toBe("3m 23s");
    // Not "unknown", not "0s": an absent number renders as nothing at all.
    expect(formatFinishElapsed(null)).toBe("");
  });
});

describe("rendering", () => {
  it("renders nothing at all for a task no finish has ever run for", () => {
    const { container } = render(<FinishPanel finish={null} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("shows the steps, the one in flight, and the gate's position", () => {
    render(<FinishPanel finish={finish()} />);

    expect(screen.getByRole("region", { name: "Finish" })).toHaveAttribute(
      "data-finish-state",
      "running",
    );
    expect(screen.getByText("Finishing this task")).toBeInTheDocument();
    const gate = document.querySelector('[data-finish-step="gate"]');
    expect(gate).toHaveAttribute("data-step-state", "running");
    expect(gate?.textContent).toContain("Gate: pytest — 6 of 10");
  });

  it("explains that a running finish has no text, rather than showing an empty box", () => {
    render(<FinishPanel finish={finish()} />);
    expect(screen.getByText(/writes its output when the process ends/)).toBeInTheDocument();
  });

  it("shows the output once the process has written it", () => {
    render(
      <FinishPanel
        finish={finish({
          state: "finished",
          live: false,
          merge_commit: "7733b2b0",
          output_source: "finish-log",
          output_tail: "task-296: finished (finished)\n  gate       ok  173.9s",
        })}
      />,
    );

    // Collapsed for a finish that was already over when the page loaded: a task with
    // an old finish must not open as a wall of terminal output.
    expect(document.querySelector("[data-finish-output]")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /Output/ }));
    expect(document.querySelector("[data-finish-output]")?.textContent).toContain("173.9s");
  });

  it("opens itself while a finish is live", () => {
    render(
      <FinishPanel
        finish={finish({ output_source: "gate-log", output_tail: "pytest ... 300 passed" })}
      />,
    );
    expect(document.querySelector("[data-finish-output]")?.textContent).toContain("300 passed");
  });
});
