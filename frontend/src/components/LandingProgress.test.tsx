import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { EstimatePoint, EstimatorState, LandingEstimate, TaskFinishView } from "../api/types";
import { FinishPanel } from "./FinishPanel";
import { LandingBar, LandingProgress, counted, landingSentence, landingWords } from "./LandingProgress";
import { estimateReadout, estimatorWords } from "./analyticsSecondSet";

/**
 * The landing estimate as a reader sees it (task-586).
 *
 * The numbers come from the server; what is tested here is that they are said the way
 * the task's constraints require -- never done before the finish is, never a negative,
 * never an invented runway wait -- and that the bar a screen reader reads carries the
 * same value the server sent.
 */

function estimate(overrides: Partial<LandingEstimate> = {}): LandingEstimate {
  return {
    kind: "estimate",
    progress: 0.3754,
    eta_seconds: 203,
    overrun: false,
    basis: "Median of the last 20 finished landings and 20 green gates. An estimate, not a promise.",
    typical_seconds: 325,
    elapsed_seconds: 122,
    ...overrides,
  };
}

describe("landingWords", () => {
  it("says the time left in whole minutes", () => {
    expect(landingWords(estimate())).toBe("~3 min left");
  });

  it("never says zero minutes", () => {
    expect(landingWords(estimate({ eta_seconds: 20 }))).toBe("under a minute left");
    expect(landingWords(estimate({ eta_seconds: 0 }))).toBe("under a minute left");
  });

  it("says an overrun in words, not a number", () => {
    expect(landingWords(estimate({ overrun: true, eta_seconds: 23 }))).toBe("taking longer than usual");
  });

  it("invents no runway wait", () => {
    expect(landingWords(estimate({ kind: "runway", progress: null, eta_seconds: null }))).toBe(
      "waiting for the merge runway",
    );
  });

  it("says nothing it cannot back without history", () => {
    expect(landingWords(estimate({ kind: "no_history", progress: null, eta_seconds: null }))).toBe("");
  });
});

describe("landingSentence", () => {
  it("names the typical landing beside the estimate", () => {
    expect(landingSentence(estimate())).toBe("Estimate: about 3 min left (typical: 5 min).");
  });

  it("says a runway wait is not estimated", () => {
    expect(landingSentence(estimate({ kind: "runway", progress: null, eta_seconds: null }))).toMatch(
      /^Waiting for the merge runway/,
    );
  });
});

describe("counted", () => {
  it("counts the server's ETA down and moves the bar towards it, below the cap", () => {
    const now = counted(estimate({ progress: 0.9, eta_seconds: 10 }), 20);
    expect(now.eta).toBe(1);
    expect(now.progress).toBe(0.95);
  });

  it("does not move an overrun", () => {
    expect(counted(estimate({ overrun: true }), 20)).toEqual({ progress: 0.3754, eta: 203 });
  });

  it("stops counting once the answer is stale", () => {
    expect(counted(estimate(), 600).eta).toBe(203 - 30);
  });
});

describe("LandingBar", () => {
  it("carries the server's progress as its value", () => {
    render(<LandingBar estimate={estimate()} />);
    const bar = screen.getByRole("progressbar");
    expect(bar.getAttribute("aria-valuenow")).toBe("38");
    expect(bar.getAttribute("aria-valuetext")).toBe("~3 min left");
    expect(bar.getAttribute("title")).toMatch(/^Median of the last 20/);
  });

  it("is indeterminate on the runway", () => {
    render(<LandingBar estimate={estimate({ kind: "runway", progress: null, eta_seconds: null })} />);
    const bar = screen.getByRole("progressbar");
    expect(bar.getAttribute("aria-valuenow")).toBeNull();
    expect(bar.getAttribute("data-landing-kind")).toBe("runway");
  });
});

describe("LandingProgress", () => {
  it("shows the server's elapsed time and no bar without history", () => {
    render(
      <LandingProgress
        estimate={estimate({ kind: "no_history", progress: null, eta_seconds: null, elapsed_seconds: 250 })}
      />,
    );
    expect(screen.queryByRole("progressbar")).toBeNull();
    expect(screen.getByText("4m 10s so far, no estimate yet")).toBeTruthy();
  });

  it("renders nothing for a task with no live finish", () => {
    const { container } = render(<LandingProgress estimate={null} />);
    expect(container.textContent).toBe("");
  });
});

describe("FinishPanel", () => {
  const live = {
    task_id: "task-586",
    project_id: "agentjobs",
    state: "running",
    live: true,
    finish_id: "fin_live",
    started_at: "2026-09-25T01:00:00+00:00",
    finished_at: "",
    elapsed_seconds: 122,
    branch: "feat/task-586-landing-eta",
    worktree: "",
    current_step: "gate",
    steps: [],
    output_url: "/api/projects/agentjobs/dispatch/finishes/task-586/output",
    output_tail: "",
    output_source: "none",
  } as unknown as TaskFinishView;

  it("draws the estimate above the steps, with its basis as the tooltip", () => {
    render(<FinishPanel finish={{ ...live, estimate: estimate() }} />);
    expect(screen.getByText("Estimate: about 3 min left (typical: 5 min).")).toBeTruthy();
    expect(screen.getByRole("progressbar").getAttribute("aria-valuenow")).toBe("38");
  });

  it("says an overrun rather than a number", () => {
    render(<FinishPanel finish={{ ...live, estimate: estimate({ overrun: true }) }} />);
    expect(screen.getByText("Taking longer than usual (typical: 5 min).")).toBeTruthy();
  });
});

describe("the accuracy readout", () => {
  const point: EstimatePoint = {
    bucket: "2026-09-21",
    sample: 5,
    error_p50_pct: 8.4,
    raw_error_p50_pct: 31.2,
    within_20: 0.8,
    bias_p50: 1.34,
    outliers: 1,
  };

  it("says how far off the shown estimate was, and what the correction bought", () => {
    const words = estimateReadout(point, "week");
    expect(words).toContain("5 landings");
    expect(words).toContain("the estimate shown was off by 8% (median)");
    expect(words).toContain("80% landed within ±20% of it");
    expect(words).toContain("uncorrected it would have been 31% off");
    expect(words).toContain("correction ×1.34");
    expect(words).toContain("1 outlier (gate retry or runway wait)");
  });

  it("says when the clamp is holding the correction", () => {
    const state: EstimatorState = {
      factor: 2,
      active: true,
      sample: 6,
      learned: 3.1,
      clamped: true,
      excluded: 0,
      reset_at: null,
      floor: 0.5,
      ceiling: 2,
      min_sample: 3,
      window: 20,
    };
    expect(estimatorWords(state)).toBe(
      "Estimates are multiplied by ×2.00, the median miss of the last 6 measured landings. The measured miss is ×3.10, held at the 0.5–2 clamp.",
    );
  });

  it("says no correction is applied before there is enough to learn from", () => {
    expect(
      estimatorWords({ factor: 1, active: false, sample: 1, floor: 0.5, ceiling: 2, min_sample: 3, window: 20 }),
    ).toBe("No correction applied yet: it needs 3 measured landings and has 1.");
  });
});
