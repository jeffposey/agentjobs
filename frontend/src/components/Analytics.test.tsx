import { fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import type { AnalyticsResponse, BacklogPoint, ThroughputPoint } from "../api/types";
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
    cycle_p50_days: null,
    cycle_p90_days: null,
    sample: 0,
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
      throughput_bucket: "week",
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
      throughputPoint("2026-09-07", {
        tasks_completed: 9,
        completion_events: 10,
        cancelled: 1,
        cycle_p50_days: 3.2,
        cycle_p90_days: 8.4,
        sample: 9,
      }),
      throughputPoint("2026-09-14", {
        tasks_completed: 2,
        completion_events: 2,
        cancelled: 0,
        cycle_p50_days: 1.1,
        cycle_p90_days: 1.9,
        sample: 2,
      }),
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
    stuck: [
      {
        ball: "human",
        ball_reason: "spec",
        tasks: 29,
        mean_days_held: 23.72,
        max_days_held: 34.2,
        oldest_task_id: "task-101",
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

describe("the page's six regions", () => {
  it("renders them in the order of the four questions (ac-1)", () => {
    renderPage(fullHistory());
    const regions = screen
      .getAllByRole("region")
      .map((region) => region.getAttribute("aria-label"));
    expect(regions).toEqual([
      "Counts and their change",
      "Backlog",
      "Throughput and cycle time",
      "Aging",
      "The ten oldest open tasks",
      "Stuck",
      "What this page can claim",
    ]);
  });

  it("says what it is doing rather than rendering an empty page while the request is out", () => {
    renderPage(null);
    expect(screen.getByText(/Reading this project's history/)).toBeInTheDocument();
  });
});

describe("the summary row (ac-2)", () => {
  it("shows each count with its change over the range", () => {
    renderPage(fullHistory());
    const open = screen.getByTestId("summary-open");
    expect(open).toHaveTextContent("149");
    // 16 opened less 6 closed across the three buckets.
    expect(open).toHaveTextContent("▲ 10 more");
  });

  it("says the direction in words as well as in the glyph and the colour", () => {
    // §8.2: red-up is bad for backlog and good for completed, so colour alone cannot
    // carry it. The accessible name is what a screen reader gets, and it is the same
    // sentence a sighted reader sees.
    renderPage(fullHistory());
    expect(screen.getByTestId("summary-external")).toHaveTextContent("▼ 3 fewer");
    expect(screen.getByRole("link", { name: /Completed: 273/ })).toHaveAccessibleName(
      /\+11 since 21 Jun 2026/,
    );
  });

  it("attributes the change to the window it really covers, not the one asked for", () => {
    // §9.3: `coverage.complete` is false here, so "in 90 days" would claim a window
    // the store cannot answer for.
    renderPage(fullHistory());
    expect(screen.getByTestId("summary-open")).toHaveTextContent("since 21 Jun 2026");
  });

  it("uses the nominal window once coverage reaches all of it", () => {
    const data = fullHistory();
    renderPage({ ...data, coverage: { ...data.coverage, complete: true } });
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

describe("the backlog panel (ac-3)", () => {
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
    expect(screen.getByTestId("reconstructed-span")).toBeInTheDocument();
  });

  it("hatches nothing when every bucket is exact", () => {
    const data = fullHistory();
    renderPage({
      ...data,
      backlog: (data.backlog ?? []).map((point) => ({ ...point, estimated: false })),
    });
    expect(screen.queryByTestId("reconstructed-span")).not.toBeInTheDocument();
  });

  it("names what it shows and its headline value", () => {
    renderPage(fullHistory());
    expect(screen.getByTestId("backlog-chart")).toHaveAccessibleName(
      "Open tasks over time, ending at 149, with tasks opened and closed in each bucket.",
    );
  });
});

describe("every chart exposes its series (ac-4)", () => {
  it("gives a hidden table to each of the four", () => {
    renderPage(fullHistory());
    for (const id of ["backlog-series", "throughput-series", "aging-series", "holders-series"]) {
      expect(screen.getByTestId(id)).toHaveClass("sr-only");
    }
  });

  it("says a percentile is not measured rather than printing a zero", () => {
    // §9.3, and §8.4's rule that a bucket under three completions has no percentile.
    // A zero there would be a two-day median that nobody computed.
    const rows = (() => {
      renderPage(fullHistory());
      return seriesRows("throughput-series");
    })();
    expect(rows[0]).toEqual(["week of 7 Sep", "9", "10", "1", "3.2d", "8.4d"]);
    expect(rows[1]).toEqual(["week of 14 Sep", "2", "2", "0", "not measured", "not measured"]);
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

describe("the three states (ac-5)", () => {
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
    // The percentile line is a trend; the completion bars beside it are not.
    expect(screen.queryByTestId("cycle-p50")).not.toBeInTheDocument();
    expect(screen.getAllByTestId("completed-bar").length).toBeGreaterThan(0);
    expect(screen.getByTestId("throughput-readout")).not.toHaveTextContent("median");
  });

  it("carries the coverage sentence on an unknown history, always", () => {
    renderPage(fullHistory());
    const footer = screen.getByTestId("coverage-footer");
    expect(footer).toHaveTextContent("History from 25 Oct 2025");
    expect(footer).toHaveTextContent("Buckets are days in America/Chicago.");
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

  it("mentions a reopening in the readout only where one happened", () => {
    renderPage(fullHistory());
    const chart = screen.getByTestId("throughput-chart");
    fireEvent.click(within(chart).getByLabelText(/week of 7 Sep/));
    expect(screen.getByTestId("throughput-readout")).toHaveTextContent(
      "10 completion events — a task was reopened",
    );
    fireEvent.click(within(chart).getByLabelText(/week of 14 Sep/));
    expect(screen.getByTestId("throughput-readout")).not.toHaveTextContent("completion events");
  });
});

describe("the aging and stuck panels", () => {
  it("links each of the oldest open tasks to its record", () => {
    renderPage(fullHistory());
    const oldest = within(screen.getByTestId("oldest-tasks"));
    expect(oldest.getByRole("link", { name: "Schema v2: CLI mirrors" })).toHaveAttribute(
      "href",
      "/p/demo/tasks/task-053",
    );
    expect(screen.getByTestId("oldest-tasks")).toHaveTextContent("51 days, with an agent");
  });

  it("reads a stuck group as a sentence in the app's own vocabulary", () => {
    renderPage(fullHistory());
    expect(screen.getByTestId("stuck-groups")).toHaveTextContent(
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
