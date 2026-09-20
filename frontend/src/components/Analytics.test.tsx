import { fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import type {
  AnalyticsResponse,
  BacklogPoint,
  CostPerTaskPoint,
  FinishPoint,
  GatePoint,
  RunPoint,
  ReviewPoint,
  SegmentPoint,
  ThroughputPoint,
} from "../api/types";
import { Analytics } from "./Analytics";

/**
 * The analytics page, against seeded fixtures for each of §9's three states.
 *
 * What is asserted here is what a reader will act on: the rendered number, the words
 * beside it, the rows of the visually-hidden series table. Never a path's `d`
 * attribute -- a coordinate string is not what anybody reads, and a test that pinned
 * one would break on every cosmetic change while catching none of the mistakes this
 * page can actually make.
 *
 * What jsdom cannot answer is deliberately absent: it reports zeros for every
 * rectangle, so "does it fit at 390px" and "does a real tap land" are measured in a
 * browser instead, in `e2e/analytics.spec.ts`. The charts are built so that the split
 * is honest -- the geometry is pure functions with their own tests, and the tap target
 * is a real element rather than arithmetic on a measured box.
 */

const NOW = new Date("2026-09-18T18:50:00Z");

function backlogPoint(day: string, over: Partial<BacklogPoint> = {}): BacklogPoint {
  return { day, open_count: 10, opened: 0, closed: 0, estimated: false, ...over };
}

function throughputPoint(bucket: string, over: Partial<ThroughputPoint> = {}): ThroughputPoint {
  return {
    bucket,
    tasks_completed: 0,
    completion_events: 0,
    cancelled: 0,
    reopened: 0,
    estimated: false,
    ...over,
  };
}

function segmentPoint(bucket: string, over: Partial<SegmentPoint> = {}): SegmentPoint {
  return {
    bucket,
    sample: 0,
    excluded: 0,
    unreviewed: 0,
    first_review_sample: 0,
    estimated: false,
    among: {},
    ...over,
  };
}

/** A project with months of history, a reconstructed span in it, and real work. */
function fullHistory(over: Partial<AnalyticsResponse> = {}): AnalyticsResponse {
  return {
    range: {
      key: "90d",
      start: "2026-06-21T05:00:00Z",
      end: "2026-09-18T18:50:00Z",
      bucket: "day",
      // §19.2: throughput is the spine grain now, so the two agree.
      throughput_bucket: "day",
      timezone: "America/Chicago",
    },
    coverage: {
      baseline_at: "2025-10-25T19:00:00Z",
      baseline_kind: "backfilled",
      native_from: "2026-09-07T19:06:00Z",
      reconstructed_before: "2026-09-07T19:01:00Z",
      events: { native: 382, reconstructed: 1604, backfilled: 446 },
      complete: false,
      note: "History from 25 Oct 2025. Events before 7 Sep 2026 were reconstructed.",
    },
    totals: {
      total: 453,
      in_progress: 3,
      blocked: 2,
      waiting_for_human: 0,
      awaiting_input: 29,
      completed: 273,
      open: 149,
    },
    backlog: [
      backlogPoint("2026-09-16", { open_count: 140, opened: 1, closed: 0, estimated: true }),
      backlogPoint("2026-09-17", { open_count: 147, opened: 7, closed: 0 }),
      backlogPoint("2026-09-18", { open_count: 149, opened: 8, closed: 6 }),
    ],
    holders: [
      { day: "2026-09-16", agent: 110, human: 25, external: 5 },
      { day: "2026-09-17", agent: 114, human: 28, external: 5 },
      { day: "2026-09-18", agent: 118, human: 29, external: 2 },
    ],
    throughput: [
      throughputPoint("2026-09-16", { tasks_completed: 9, completion_events: 10, cancelled: 1, reopened: 1 }),
      throughputPoint("2026-09-17", { tasks_completed: 0, completion_events: 0 }),
      throughputPoint("2026-09-18", { tasks_completed: 2, completion_events: 2 }),
    ],
    segments: [
      segmentPoint("2026-09-07", {
        sample: 22,
        excluded: 1,
        unreviewed: 12,
        queue_p50_hours: 1.1,
        queue_p90_hours: 96,
        work_p50_hours: 0.6,
        work_p90_hours: 1.5,
        waiting_p50_hours: 0,
        waiting_p90_hours: 2.1,
        review_p50_hours: 0,
        review_p90_hours: 0.2,
        finish_p50_hours: 0,
        finish_p90_hours: 0.1,
        total_p50_hours: 4.8,
        total_p90_hours: 300,
        first_review_p50_hours: 0.8,
        first_review_p90_hours: 4,
        first_review_sample: 9,
      }),
      // Two completions: under the minimum sample, so no median exists for it.
      segmentPoint("2026-09-14", { sample: 2 }),
    ],
    segments_coverage: { bucket: "week", complete: false, recorded_from: "2025-10-25T19:00:00Z", note: null },
    finishes: [
      {
        bucket: "2026-09-07",
        finished: 7,
        escalated: 2,
        declined: 0,
        interrupted: 1,
        reasons: { gate_failed: 2 },
        duration_p50_min: 4.8,
        duration_p90_min: 8.3,
        sample: 7,
        steps_p50_s: { gate: 253, merge: 1.2 },
        runway_waited: 2,
        runway_p90_s: 359,
        estimated: false,
      } satisfies FinishPoint,
      {
        bucket: "2026-09-14",
        finished: 3,
        escalated: 0,
        declined: 0,
        interrupted: 0,
        reasons: {},
        duration_p50_min: 5.2,
        duration_p90_min: 6.1,
        sample: 3,
        steps_p50_s: {},
        runway_waited: 0,
        runway_p90_s: null,
        estimated: false,
      } satisfies FinishPoint,
    ],
    finishes_coverage: {
      bucket: "week",
      complete: false,
      recorded_from: "2026-08-23T00:00:00Z",
      note: "Finishes are recorded from 23 Aug 2026.",
    },
    gates: [
      {
        bucket: "2026-09-07",
        full: 9,
        passed: 7,
        failed_stages: { pytest: 2 },
        duration_p50_min: 4.0,
        duration_p90_min: 6.1,
        sample: 7,
        stages_p50_s: { pytest: 125, e2e: 107 },
        origins: { finish: 7, run: 2 },
        estimated: false,
      } satisfies GatePoint,
      {
        bucket: "2026-09-14",
        full: 3,
        passed: 3,
        failed_stages: {},
        duration_p50_min: 3.6,
        duration_p90_min: 4.2,
        sample: 3,
        stages_p50_s: { pytest: 120 },
        origins: { finish: 3 },
        estimated: false,
      } satisfies GatePoint,
    ],
    gates_coverage: {
      bucket: "week",
      complete: false,
      recorded_from: "2026-08-23T00:00:00Z",
      note: "Agent-side gates are recorded only from 19 Sep 2026.",
    },
    runs: [
      {
        bucket: "2026-09-16",
        runs: 12,
        triggers: { manual: 9, child: 3 },
        agent_hours: 6.4,
        outcomes: { completed: 10, interrupted: 2 },
        in_flight: 0,
        duration_p50_min: 27,
        duration_p90_min: 97,
        sample: 12,
        estimated: false,
      } satisfies RunPoint,
      {
        bucket: "2026-09-17",
        runs: 4,
        triggers: { manual: 4 },
        agent_hours: 1.2,
        outcomes: { completed: 3, cancelled: 1 },
        in_flight: 1,
        duration_p50_min: 18,
        duration_p90_min: 30,
        sample: 3,
        estimated: false,
      } satisfies RunPoint,
    ],
    runs_coverage: {
      bucket: "day",
      complete: false,
      recorded_from: "2026-09-07T19:06:00Z",
      note: "Runs are recorded from 7 Sep 2026.",
    },
    machine: [
      {
        bucket: "2026-09-16",
        admitted: 12,
        start_latency_p50_s: 2.2,
        start_latency_p90_s: 3.1,
        queued: 2,
        queue_wait_p50_s: 90,
        queue_wait_p90_s: 140,
        paused_run_hours: 6.9,
        paused_waiters: 5,
      },
    ],
    machine_coverage: {
      bucket: "day",
      complete: false,
      recorded_from: "2026-09-13T00:00:00Z",
      note: "The execution journal is recorded from 13 Sep 2026.",
    },
    review: [
      {
        bucket: "2026-09-07",
        exits: 34,
        approvals: 24,
        wait_p50_hours: 0.07,
        wait_p90_hours: 5.1,
        first_time_approvals: 14,
        questions: 5,
        answered: 4,
        answer_p50_hours: 0.27,
        answer_p90_hours: 30,
        estimated: false,
      } satisfies ReviewPoint,
      {
        bucket: "2026-09-14",
        exits: 2,
        approvals: 2,
        wait_p50_hours: 0.5,
        wait_p90_hours: 0.9,
        first_time_approvals: 2,
        questions: 0,
        answered: 0,
        answer_p50_hours: null,
        answer_p90_hours: null,
        estimated: false,
      } satisfies ReviewPoint,
    ],
    review_coverage: { bucket: "week", complete: false, recorded_from: "2025-10-25T19:00:00Z", note: null },
    cost_per_task: [
      {
        bucket: "2026-09-07",
        sample: 22,
        runs_mean: 1.4,
        runs_mode: 1,
        finishes_mean: 1.3,
        gate_minutes_p50: 4.4,
        gate_minutes_p90: 6.9,
        without_gate: 2,
        estimated: false,
      } satisfies CostPerTaskPoint,
      {
        bucket: "2026-09-14",
        sample: 0,
        runs_mean: null,
        runs_mode: null,
        finishes_mean: null,
        gate_minutes_p50: null,
        gate_minutes_p90: null,
        without_gate: 0,
        estimated: false,
      } satisfies CostPerTaskPoint,
    ],
    cost_coverage: { bucket: "week", complete: false, recorded_from: "2026-08-23T00:00:00Z", note: null },
    in_review: [
      { task_id: "task-231", title: "The review queue, reordered", hours_waiting: 5.4 },
      { task_id: "task-240", title: "A second thing to look at", hours_waiting: 0.3 },
    ],
    open_questions: [
      { task_id: "task-409", entry_id: 7, hours_open: 52 },
    ],
    aging: [
      { label: "0-6d", tasks: 25, mean_age_days: 4.84 },
      { label: "7-29d", tasks: 80, mean_age_days: 20.04 },
      { label: "30-89d", tasks: 44, mean_age_days: 32.21 },
      { label: "90d+", tasks: 0, mean_age_days: 0 },
    ],
    oldest: [
      {
        task_id: "task-053",
        title: "Schema v2: CLI mirrors",
        priority: "medium",
        ball: "agent",
        ball_reason: "available",
        age_days: 50.92,
      },
    ],
    // Deliberately out of §19.3's order, and with the queue holding the most tasks --
    // which is what the API's own count-descending sort returns and what the page has
    // to reorder.
    stuck: [
      {
        ball: "agent",
        ball_reason: "available",
        tasks: 96,
        mean_days_held: 12.1,
        max_days_held: 40.5,
        oldest_task_id: "task-053",
      },
      {
        ball: "human",
        ball_reason: "spec",
        tasks: 29,
        mean_days_held: 23.72,
        max_days_held: 34.2,
        oldest_task_id: "task-101",
      },
      {
        ball: "agent",
        ball_reason: "work",
        tasks: 3,
        mean_days_held: 0.4,
        max_days_held: 1.1,
        oldest_task_id: "task-474",
      },
      {
        ball: "external",
        ball_reason: "dependency",
        tasks: 2,
        mean_days_held: 6.0,
        max_days_held: 9.3,
        oldest_task_id: "task-207",
      },
    ],
    ...over,
  };
}

/** §9.1: a project initialised today. Totals populated, every series empty. */
function noHistory(): AnalyticsResponse {
  return fullHistory({
    coverage: {
      baseline_at: null,
      baseline_kind: "unknown",
      native_from: null,
      reconstructed_before: null,
      events: {},
      complete: false,
      note: null,
    },
    totals: {
      total: 2,
      in_progress: 0,
      blocked: 0,
      waiting_for_human: 0,
      awaiting_input: 0,
      completed: 0,
      open: 2,
    },
    backlog: [],
    holders: [],
    throughput: [],
    segments: [],
    finishes: [],
    gates: [],
    runs: [],
    machine: [],
    review: [],
    cost_per_task: [],
    in_review: [],
    open_questions: [],
    aging: [],
    oldest: [],
    stuck: [],
  });
}

/** §9.2: nine days old. The values are facts; the trends are not yet. */
function thinHistory(): AnalyticsResponse {
  const base = fullHistory();
  return {
    ...base,
    range: { ...base.range, start: "2026-09-09T05:00:00Z" },
    coverage: {
      ...base.coverage,
      baseline_at: "2026-09-09T12:00:00Z",
      baseline_kind: "native",
      native_from: "2026-09-09T12:00:00Z",
      reconstructed_before: null,
      complete: true,
      note: "History from 9 Sep 2026.",
    },
  };
}

function renderPage(data: AnalyticsResponse | null, onRangeChange = vi.fn()) {
  return render(
    <MemoryRouter initialEntries={["/p/demo/analytics"]}>
      <Analytics
        data={data}
        projectId="demo"
        rangeKey="90d"
        onRangeChange={onRangeChange}
        now={NOW}
      />
    </MemoryRouter>,
  );
}

/** The rows of a chart's visually-hidden series table, as a component test reads them. */
function seriesRows(testId: string): string[][] {
  const body = screen.getByTestId(testId).querySelectorAll("tbody tr");
  return Array.from(body, (row) =>
    Array.from(row.querySelectorAll("th, td"), (cell) => cell.textContent ?? ""),
  );
}

describe("the page's panels (ac-1, ac-2)", () => {
  it("renders them in §19.4's order", () => {
    renderPage(fullHistory());
    const regions = screen
      .getAllByRole("region")
      .map((region) => region.getAttribute("aria-label"));
    expect(regions).toEqual([
      "Counts and their change",
      "Backlog",
      "Throughput",
      "Where the time goes",
      "Review and questions",
      "Finishes and gates",
      "Runs",
      "Cost per completed task",
      "Aging",
      "The ten oldest open tasks",
      "Stuck",
      "What this page can claim",
    ]);
  });

  it("puts the calls to action above the machine, and the right-now lists last", () => {
    // §19.4's argument: "Review" is a list of things waiting on the reader, and a call
    // to action belongs above a report. Aging and stuck are snapshots, and the owner's
    // ask was trends.
    renderPage(fullHistory());
    const order = screen
      .getAllByRole("region")
      .map((region) => region.getAttribute("aria-label") ?? "");
    expect(order.indexOf("Review and questions")).toBeLessThan(order.indexOf("Runs"));
    expect(order.indexOf("Runs")).toBeLessThan(order.indexOf("Stuck"));
  });

  it("says what it is doing rather than rendering an empty page while the request is out", () => {
    renderPage(null);
    expect(screen.getByText(/Reading this project's history/)).toBeInTheDocument();
  });
});

describe("the summary row (ac-3)", () => {
  it("shows each count with its change over the range", () => {
    renderPage(fullHistory());
    const open = screen.getByTestId("summary-open");
    expect(open).toHaveTextContent("149");
    // 16 opened less 6 closed across the three buckets.
    expect(open).toHaveTextContent("▲ 10 more");
  });

  it("names the date native history begins, not the backfilled floor (§19.1)", () => {
    renderPage(fullHistory());
    expect(screen.getByTestId("summary-open")).toHaveTextContent("since 7 Sep 2026");
    expect(screen.getByTestId("summary-open")).not.toHaveTextContent("Jun");
  });

  it("shows no tile whose delta silently equals its own count", () => {
    // The failure section 19.1 replaces: a 90-day window differenced against an
    // October 2025 backfill reported *"149 open with 145 more"*, which is the total
    // with an arrow on it. Asserted over every tile rather than over the one that
    // failed -- and it is the *silent* form that is the defect, so a rise the page
    // labels as the whole count is allowed by the next test and by this one.
    renderPage(fullHistory());
    for (const key of ["open", "created", "completed", "human", "external"]) {
      const tile = screen.getByTestId(`summary-${key}`);
      const count = tile.querySelector("span:nth-of-type(2)")?.textContent ?? "";
      const change = tile.querySelector("span:nth-of-type(3)")?.textContent ?? "";
      const restated = new RegExp("(^|[^0-9])" + count + "([^0-9]|$)").test(change);
      expect(
        restated && !change.includes("all of them"),
        `${key} restated its own total: "${change}"`,
      ).toBe(false);
    }
  });

  it("says so where a rise really is the whole count, rather than leaving it ambiguous", () => {
    // Against a native baseline this shape is sometimes simply true: every blocked
    // task became blocked inside the window. The tile is then unreadable only if it
    // does not say which of the two it is.
    const data = fullHistory();
    renderPage({
      ...data,
      totals: { ...data.totals, blocked: 3 },
      holders: [
        { day: "2026-09-16", agent: 110, human: 25, external: 0 },
        { day: "2026-09-18", agent: 118, human: 29, external: 3 },
      ],
    });
    expect(screen.getByTestId("summary-external")).toHaveTextContent(
      "3 more since 7 Sep 2026 — all of them",
    );
  });

  it("says the direction in words as well as in the glyph and the colour", () => {
    // §8.2: red-up is bad for backlog and good for completed, so colour alone cannot
    // carry it. The accessible name is what a screen reader gets, and it is the same
    // sentence a sighted reader sees.
    renderPage(fullHistory());
    expect(screen.getByTestId("summary-external")).toHaveTextContent("▼ 3 fewer");
    expect(screen.getByRole("link", { name: /Completed: 273/ })).toHaveAccessibleName(
      /\+11 since 7 Sep 2026/,
    );
  });

  it("uses the nominal window once native history is older than it", () => {
    const data = fullHistory();
    renderPage({
      ...data,
      coverage: { ...data.coverage, native_from: "2026-01-01T00:00:00Z", complete: true },
    });
    expect(screen.getByTestId("summary-open")).toHaveTextContent("in 90 days");
  });

  it("leads each count to the Tasks filter that shows it", () => {
    renderPage(fullHistory());
    expect(screen.getByRole("link", { name: /^Waiting on you: 29/ })).toHaveAttribute(
      "href",
      "/p/demo/tasks?status=human",
    );
  });
});

describe("the backlog panel", () => {
  it("draws the level and both flows against a zero-based axis", () => {
    renderPage(fullHistory());
    expect(seriesRows("backlog-series")).toEqual([
      ["16 Sep", "140", "1", "0", "reconstructed"],
      ["17 Sep", "147", "7", "0", "yes"],
      ["18 Sep", "149", "8", "6", "yes"],
    ]);
    // The axis names its own zero, which is what stops a 3% change reading as a cliff.
    const chart = screen.getByTestId("backlog-chart");
    expect(within(chart).getByText("0")).toBeInTheDocument();
    expect(within(chart).getByText("200")).toBeInTheDocument();
  });

  it("hatches the reconstructed span rather than drawing it as though it were measured", () => {
    renderPage(fullHistory());
    expect(screen.getAllByTestId("reconstructed-span").length).toBeGreaterThan(0);
  });

  it("names what it shows and its headline value", () => {
    renderPage(fullHistory());
    expect(screen.getByTestId("backlog-chart")).toHaveAccessibleName(
      "Open tasks over time, ending at 149, with tasks opened and closed in each bucket.",
    );
  });
});

describe("throughput, on its own axis (ac-1)", () => {
  it("carries no cycle-time column at all", () => {
    // §19.2. The percentile line, its band and the second axis are gone; the fields
    // they were drawn from were removed from the response by the same change, so a
    // column here would have nothing behind it.
    renderPage(fullHistory());
    const header = Array.from(
      screen.getByTestId("throughput-series").querySelectorAll("thead th"),
      (cell) => cell.textContent,
    );
    expect(header).toEqual([
      "Bucket",
      "Completed",
      "Completion events",
      "Cancelled",
      "Reopened",
    ]);
    expect(screen.queryByTestId("cycle-p50")).not.toBeInTheDocument();
    expect(screen.getByTestId("throughput-readout")).not.toHaveTextContent("median");
  });

  it("draws no label inside its plot area", () => {
    // §19.2's other half: the first page printed *"median cycle, 0-40d"* over its own
    // bars. Every label now sits in the padding, above or beside the plot rectangle.
    renderPage(fullHistory());
    const chart = screen.getByTestId("throughput-chart");
    const labels = Array.from(chart.querySelectorAll("text"), (node) => ({
      text: node.textContent ?? "",
      x: Number(node.getAttribute("x")),
      y: Number(node.getAttribute("y")),
    }));
    // COUNT_PADDING against the 400x166 viewBox: the plot is x 28..392, y 18..148.
    // A label may sit in any margin; what it may not do is land in that rectangle.
    const inside = labels.filter(
      (label) => label.x > 28 && label.x < 392 && label.y > 18 && label.y < 148,
    );
    expect(
      inside.map((label) => `"${label.text}" at ${label.x},${label.y}`),
      "every label is in a margin, not over the data",
    ).toEqual([]);
  });

  it("marks a reopening without counting it as a completion", () => {
    renderPage(fullHistory());
    expect(screen.getAllByTestId("reopened-marker")).toHaveLength(1);
    expect(seriesRows("throughput-series")[0]).toEqual(["16 Sep", "9", "10", "1", "1"]);
  });

  it("is bucketed at the spine grain, so a 30-day range is completed-per-day", () => {
    renderPage(fullHistory());
    expect(screen.getByTestId("throughput-readout")).toHaveTextContent("18 Sep · 2 completed");
    expect(screen.getByTestId("throughput-readout")).not.toHaveTextContent("week of");
  });
});

describe("where the time goes (ac-2)", () => {
  it("renders the five segments with their medians and the total beside them", () => {
    renderPage(fullHistory());
    expect(seriesRows("segments-series")[0]).toEqual([
      "week of 7 Sep",
      "1.1 h",
      "36 min",
      "0",
      "0",
      "0",
      "4.8 h",
      "12.5 d",
      "22",
    ]);
  });

  it("leaves a bucket under the minimum sample blank rather than drawing five noughts", () => {
    renderPage(fullHistory());
    expect(seriesRows("segments-series")[1]).toEqual([
      "week of 14 Sep",
      "not measured",
      "not measured",
      "not measured",
      "not measured",
      "not measured",
      "not measured",
      "not measured",
      "2",
    ]);
    expect(screen.getAllByTestId("blank-buckets").length).toBeGreaterThan(0);
  });

  it("carries the 90th percentile in the readout, which is what a tap reaches", () => {
    // §19.5: the p50-to-p90 band is gone and the tap replaced it. The readout always
    // describes the selected bucket, so a tap is how a reader gets the p90.
    renderPage(fullHistory());
    const chart = screen.getByTestId("segments-chart");
    fireEvent.click(within(chart).getByLabelText(/week of 7 Sep/));
    const readout = screen.getByTestId("segments-readout");
    expect(readout).toHaveTextContent("queue 1.1 h/4.0 d");
    expect(readout).toHaveTextContent("total p50 4.8 h · p90 12.5 d");
    expect(readout).toHaveTextContent("22 tasks");
  });

  it("says once that the stack is not the median total", () => {
    renderPage(fullHistory());
    expect(screen.getByLabelText("Where the time goes")).toHaveTextContent(
      "five medians added together, which is not the median total",
    );
  });
});

describe("review and questions (ac-2)", () => {
  it("lists what is waiting on the reader, with how long it has waited", () => {
    renderPage(fullHistory());
    const list = within(screen.getByTestId("in-review"));
    expect(list.getByRole("link", { name: "The review queue, reordered" })).toHaveAttribute(
      "href",
      "/p/demo/tasks/task-231",
    );
    expect(screen.getByTestId("in-review")).toHaveTextContent("waiting 5.4 h");
    expect(screen.getByTestId("in-review")).toHaveTextContent("waiting 18 min");
  });

  it("counts the open questions in the panel's heading and lists them", () => {
    renderPage(fullHistory());
    expect(screen.getByTestId("open-questions-count")).toHaveTextContent("1 open question");
    expect(screen.getByTestId("open-questions")).toHaveTextContent("unanswered for 2 days");
  });

  it("carries the approval latency, the first-time rate and the answer wait", () => {
    renderPage(fullHistory());
    const chart = screen.getByTestId("review-chart");
    fireEvent.click(within(chart).getByLabelText(/week of 7 Sep/));
    const readout = screen.getByTestId("review-readout");
    expect(readout).toHaveTextContent("34 reviews answered · waited p50 4 min · p90 5.1 h");
    expect(readout).toHaveTextContent("14 of 24 approved first time");
    expect(readout).toHaveTextContent("5 questions asked, 4 answered");
  });
});

describe("finishes and gates (ac-2)", () => {
  it("splits finishes by outcome and names the escalation reasons", () => {
    renderPage(fullHistory());
    expect(seriesRows("finishes-series")[0]).toEqual([
      "week of 7 Sep",
      "7",
      "2",
      "0",
      "1",
      "4.8 min",
      "8.3 min",
    ]);
    const chart = screen.getByTestId("finishes-chart");
    fireEvent.click(within(chart).getByLabelText(/week of 7 Sep/));
    expect(screen.getByTestId("finishes-readout")).toHaveTextContent("escalations: gate_failed 2");
    expect(screen.getByTestId("finishes-readout")).toHaveTextContent(
      "2 waited for the runway, p90 6.0 min",
    );
  });

  it("puts the step split and the stage split in the readout, on tap", () => {
    // F3 and G2: not charts. A value that is `gate 253 s and everything else under two
    // seconds` is a list, and a chart of it would be one bar and eleven slivers.
    renderPage(fullHistory());
    const chart = screen.getByTestId("durations-chart");
    fireEvent.click(within(chart).getByLabelText(/week of 7 Sep/));
    expect(screen.getByTestId("durations-readout")).toHaveTextContent("steps: gate 4.2 min");
    expect(screen.getByTestId("durations-readout")).toHaveTextContent(
      "stages: pytest 2.1 min, e2e 1.8 min",
    );
    expect(screen.getByTestId("durations-readout")).toHaveTextContent("7 green (78%)");
  });

  it("draws the finish and its gate on one axis, so the difference is readable", () => {
    renderPage(fullHistory());
    expect(screen.getByTestId("finish-duration-line")).toBeInTheDocument();
    expect(screen.getByTestId("gate-duration-line")).toBeInTheDocument();
    expect(seriesRows("durations-series")[0]).toEqual([
      "week of 7 Sep",
      "4.8 min",
      "8.3 min",
      "4.0 min",
      "6.1 min",
      "7 of 9",
    ]);
  });

  it("says when the gate series cannot speak for the whole window", () => {
    renderPage(fullHistory());
    expect(screen.getByTestId("gates-caption")).toHaveTextContent(
      "Agent-side gates are recorded only from 19 Sep 2026.",
    );
  });
});

describe("runs (ac-2)", () => {
  it("draws the runs and their hours on two axes, and the paused hours above them", () => {
    renderPage(fullHistory());
    expect(screen.getAllByTestId("run-bar").length).toBe(2);
    expect(screen.getAllByTestId("agent-hours-bar").length).toBe(2);
    expect(screen.getAllByTestId("paused-hours-bar").length).toBe(1);
    expect(seriesRows("runs-series")[0]).toEqual(["16 Sep", "12", "6.4", "6.9", "27.0 min", "0"]);
  });

  it("carries the triggers, the queue wait and the paused hours in the readout", () => {
    renderPage(fullHistory());
    const chart = screen.getByTestId("runs-chart");
    fireEvent.click(within(chart).getByLabelText(/16 Sep/));
    const readout = screen.getByTestId("runs-readout");
    expect(readout).toHaveTextContent("12 runs · 6.4 agent-hours");
    expect(readout).toHaveTextContent("triggers: manual 9, child 3");
    expect(readout).toHaveTextContent("2 queued, waited p50 1.5 min");
    expect(readout).toHaveTextContent("6.9 run-hours paused on a usage limit across 5 waiters");
  });

  it("splits the outcome mix and keeps a run still in the air out of the bars", () => {
    renderPage(fullHistory());
    expect(seriesRows("run-outcomes-series")[1]).toEqual([
      "17 Sep",
      "3",
      "0",
      "1",
      "0",
      "1",
    ]);
  });
});

describe("cost per completed task (ac-2)", () => {
  it("draws three charts, one per unit, and blanks a bucket that completed nothing", () => {
    renderPage(fullHistory());
    for (const id of ["cost-runs-chart", "cost-finishes-chart", "cost-gate-chart"]) {
      expect(screen.getByTestId(id)).toBeInTheDocument();
    }
    expect(screen.getAllByTestId("cost-runs-chart-bar")).toHaveLength(1);
    expect(seriesRows("cost-series")[1]).toEqual([
      "week of 14 Sep",
      "0",
      "—",
      "—",
      "—",
      "—",
      "0",
    ]);
  });

  it("names the owner's three numbers in one line", () => {
    renderPage(fullHistory());
    const chart = screen.getByTestId("cost-runs-chart");
    fireEvent.click(within(chart).getByLabelText(/week of 7 Sep/));
    const readout = screen.getByTestId("cost-readout");
    expect(readout).toHaveTextContent("1.4 runs each, most often 1");
    expect(readout).toHaveTextContent("1.3 finishes each");
    expect(readout).toHaveTextContent("gate p50 4.4 min · p90 6.9 min");
    expect(readout).toHaveTextContent("2 with no gate at all");
  });
});

describe("every chart exposes its series (ac-2)", () => {
  it("gives a hidden table to each panel", () => {
    renderPage(fullHistory());
    for (const id of [
      "backlog-series",
      "throughput-series",
      "segments-series",
      "review-series",
      "finishes-series",
      "durations-series",
      "runs-series",
      "run-outcomes-series",
      "cost-series",
      "aging-series",
      "holders-series",
    ]) {
      expect(screen.getByTestId(id)).toHaveClass("sr-only");
    }
  });

  it("gives each chart an aria-label naming what it shows", () => {
    renderPage(fullHistory());
    for (const id of [
      "backlog-chart",
      "throughput-chart",
      "segments-chart",
      "review-chart",
      "finishes-chart",
      "durations-chart",
      "runs-chart",
      "run-outcomes-chart",
    ]) {
      expect(screen.getByTestId(id).getAttribute("aria-label")).toBeTruthy();
    }
  });

  it("carries the mean age of every band, including an empty one", () => {
    renderPage(fullHistory());
    expect(seriesRows("aging-series")).toEqual([
      ["0-6d", "25", "4.8d"],
      ["7-29d", "80", "20.0d"],
      ["30-89d", "44", "32.2d"],
      ["90d+", "0", "0.0d"],
    ]);
  });
});

describe("per-series coverage (ac-5)", () => {
  it("says where a series that is younger than the range starts", () => {
    renderPage(fullHistory());
    const panel = screen.getByLabelText("Finishes and gates");
    expect(panel).toHaveTextContent("Finishes are recorded from 23 Aug 2026.");
  });

  it("draws no axis and says so for a series with no rows at all", () => {
    const data = fullHistory();
    renderPage({
      ...data,
      finishes: [],
      finishes_coverage: {
        bucket: "week",
        complete: false,
        recorded_from: null,
        note: "No finishes recorded yet.",
      },
    });
    expect(screen.queryByTestId("finishes-chart")).not.toBeInTheDocument();
    expect(screen.getAllByTestId("no-series")[0]).toHaveTextContent("No finishes recorded yet.");
  });

  it("gives the footer one line per source family, because five baselines are five sentences", () => {
    renderPage(fullHistory());
    const sources = screen.getByTestId("coverage-sources");
    expect(sources).toHaveTextContent("Finishes: Finishes are recorded from 23 Aug 2026.");
    expect(sources).toHaveTextContent("Runs: Runs are recorded from 7 Sep 2026.");
    expect(sources).toHaveTextContent(
      "Execution journal: The execution journal is recorded from 13 Sep 2026.",
    );
    expect(sources).toHaveTextContent("Task history: complete for this window");
  });
});

describe("the three states", () => {
  it("says nothing has happened, and does not draw an empty axis", () => {
    renderPage(noHistory());
    expect(screen.getByTestId("no-history")).toHaveTextContent("No history yet.");
    expect(screen.queryByTestId("backlog-chart")).not.toBeInTheDocument();
    // §9.1: the counts are still shown. They are facts about now, not about history.
    expect(screen.getByTestId("summary-open")).toHaveTextContent("2");
    expect(screen.getByTestId("summary-open")).toHaveTextContent("no comparison — no history yet");
  });

  it("draws a thin history's values and suppresses its trends", () => {
    // §9.2, and the distinction that matters: the bars and the level are facts, so
    // they are drawn; a nine-day trend is noise, so it is not claimed.
    renderPage(thinHistory());
    expect(screen.getByTestId("backlog-chart")).toBeInTheDocument();
    expect(screen.getByTestId("history-depth")).toHaveTextContent("9 days of history");
    expect(screen.getByTestId("summary-open")).toHaveTextContent(
      "no comparison — only 9 days of history",
    );
    expect(screen.getAllByTestId("completed-bar").length).toBeGreaterThan(0);
  });

  it("carries the coverage sentence on an unknown history, always", () => {
    renderPage(fullHistory());
    const footer = screen.getByTestId("coverage-footer");
    expect(footer).toHaveTextContent("History from 25 Oct 2025");
    expect(footer).toHaveTextContent("Buckets are days in America/Chicago");
  });

  it("never draws an impossible backlog, and says so where it had to floor one", () => {
    // §4.3: a store imported before creation times were floored opens its series
    // below zero. The floor is drawn and the fact is stated, because a plausible line
    // silently substituted for an impossible one is undetectable by the reader.
    const data = fullHistory();
    const first = (data.backlog ?? [])[0];
    renderPage({
      ...data,
      backlog: [{ ...(first as BacklogPoint), open_count: -3 }, ...(data.backlog ?? []).slice(1)],
    });
    expect(seriesRows("backlog-series")[0]?.[1]).toBe("0");
    expect(screen.getByTestId("clamp-warning")).toHaveTextContent("below zero in one bucket");
  });

  it("says nothing about clamping when the store is fine", () => {
    renderPage(fullHistory());
    expect(screen.queryByTestId("clamp-warning")).not.toBeInTheDocument();
  });
});

describe("selecting a bucket", () => {
  it("opens on the most recent bucket, because that is what a glance is for", () => {
    renderPage(fullHistory());
    expect(screen.getByTestId("backlog-readout")).toHaveTextContent(
      "18 Sep · 149 open · 8 opened · 6 closed",
    );
  });

  it("updates the readout when a bucket is tapped", () => {
    // The hit target is a real element covering the whole column, so this is the same
    // event a finger produces -- not arithmetic on a rectangle jsdom measures as zero.
    renderPage(fullHistory());
    const chart = screen.getByTestId("backlog-chart");
    const first = within(chart).getByLabelText("16 Sep · 140 open · 1 opened · 0 closed");
    fireEvent.click(first);
    expect(screen.getByTestId("backlog-readout")).toHaveTextContent(
      "16 Sep · 140 open · 1 opened · 0 closed",
    );
  });

  it("moves the selection with the arrow keys, so a pointer is not the only way in", () => {
    renderPage(fullHistory());
    const chart = screen.getByTestId("backlog-chart");
    fireEvent.keyDown(chart, { key: "ArrowLeft" });
    expect(screen.getByTestId("backlog-readout")).toHaveTextContent("17 Sep");
    fireEvent.keyDown(chart, { key: "ArrowRight" });
    expect(screen.getByTestId("backlog-readout")).toHaveTextContent("18 Sep");
  });

  it("keeps the selection inside the series at either end", () => {
    renderPage(fullHistory());
    const chart = screen.getByTestId("backlog-chart");
    for (let press = 0; press < 5; press += 1) fireEvent.keyDown(chart, { key: "ArrowLeft" });
    expect(screen.getByTestId("backlog-readout")).toHaveTextContent("16 Sep");
  });

  it("selects each panel independently, so one tap does not move eight readouts", () => {
    renderPage(fullHistory());
    const throughput = screen.getByTestId("throughput-chart");
    fireEvent.click(within(throughput).getByLabelText(/^16 Sep/));
    expect(screen.getByTestId("throughput-readout")).toHaveTextContent("16 Sep");
    expect(screen.getByTestId("backlog-readout")).toHaveTextContent("18 Sep");
  });
});

describe("the aging and stuck panels (ac-4)", () => {
  it("links each of the oldest open tasks to its record", () => {
    renderPage(fullHistory());
    const oldest = within(screen.getByTestId("oldest-tasks"));
    expect(oldest.getByRole("link", { name: "Schema v2: CLI mirrors" })).toHaveAttribute(
      "href",
      "/p/demo/tasks/task-053",
    );
    expect(screen.getByTestId("oldest-tasks")).toHaveTextContent("51 days, with an agent");
  });

  it("puts waiting-on-you and blocked above the queue, whatever the counts are", () => {
    // §19.3. The API sorts by count descending, which put *96 ready, unclaimed* at the
    // top of a panel called "Stuck" -- and a backlog waiting its turn is not stuck.
    renderPage(fullHistory());
    const bands = Array.from(
      screen.getByTestId("stuck-groups").querySelectorAll("[data-testid^='stuck-band-']"),
      (node) => node.getAttribute("data-testid"),
    );
    expect(bands).toEqual([
      "stuck-band-human",
      "stuck-band-blocked",
      "stuck-band-agent",
      "stuck-band-queue",
    ]);
  });

  it("names the queue as the queue rather than as stuck work", () => {
    renderPage(fullHistory());
    const queue = screen.getByTestId("stuck-band-queue");
    expect(queue).toHaveTextContent("The queue");
    expect(queue).toHaveTextContent("the backlog waiting its turn");
    expect(queue).toHaveTextContent("96 tasks ready, unclaimed · longest 41 days");
  });

  it("keeps §8.6's wording for work that really has stopped", () => {
    renderPage(fullHistory());
    expect(screen.getByTestId("stuck-band-human")).toHaveTextContent(
      "29 tasks waiting on you — spec · longest 34 days",
    );
  });
});

describe("the range control", () => {
  it("asks for the range that was chosen", () => {
    const onRangeChange = vi.fn();
    renderPage(fullHistory(), onRangeChange);
    fireEvent.click(screen.getByRole("button", { name: "30 days" }));
    expect(onRangeChange).toHaveBeenCalledWith("30d");
  });

  it("says which range is showing", () => {
    renderPage(fullHistory());
    expect(screen.getByRole("button", { name: "90 days" })).toHaveAttribute("aria-pressed", "true");
  });
});
