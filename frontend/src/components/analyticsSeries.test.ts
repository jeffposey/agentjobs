import { describe, expect, it } from "vitest";

import type { AnalyticsResponse, BacklogPoint, ThroughputPoint } from "../api/types";
import {
  THIN_HISTORY_DAYS,
  ageWords,
  backlogReadout,
  clampWarning,
  days,
  deltaWindow,
  formatBucket,
  formatDay,
  formatInstant,
  historyOf,
  holderReadout,
  openLevel,
  stuckPhrase,
  summaryTiles,
} from "./analyticsSeries";
import { deltaBaseline } from "./analyticsSecondSet";

/**
 * The rules the page would otherwise get wrong quietly (docs/analytics-design.md §9).
 *
 * Every assertion here is about a claim rather than about a pixel: whether a delta may
 * be shown at all, which window it is attributed to, and where a number has to be
 * replaced by a sentence. None of it needs a DOM.
 */

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

/** A payload with real history, which every test below varies one thing of. */
function response(over: Partial<AnalyticsResponse> = {}): AnalyticsResponse {
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
      note: "History from 25 Oct 2025.",
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
    // Two buckets before `native_from` and two after it. Everything the summary row
    // claims is computed from the two after, which is §19.1's whole point: the pair
    // before are backfilled, and differencing against them is what produced a delta
    // within four of its own total.
    backlog: [
      backlogPoint("2026-06-21", { open_count: 4, opened: 120, closed: 0 }),
      backlogPoint("2026-06-22", { open_count: 131, opened: 11, closed: 0 }),
      backlogPoint("2026-09-08", { open_count: 140, opened: 2, closed: 0 }),
      backlogPoint("2026-09-09", { open_count: 149, opened: 8, closed: 1 }),
    ],
    holders: [
      { day: "2026-06-21", agent: 4, human: 0, external: 0 },
      { day: "2026-06-22", agent: 100, human: 20, external: 9 },
      { day: "2026-09-08", agent: 110, human: 25, external: 5 },
      { day: "2026-09-09", agent: 118, human: 29, external: 2 },
    ],
    throughput: [
      throughputPoint("2026-06-15", { tasks_completed: 60, completion_events: 60 }),
      throughputPoint("2026-09-08", { tasks_completed: 4, completion_events: 4 }),
    ],
    aging: [],
    oldest: [],
    stuck: [],
    ...over,
  };
}

const NOW = new Date("2026-09-18T18:50:00Z");

describe("formatDay", () => {
  it("reads a bucket key as the local day the server already bucketed it in", () => {
    // Deliberately not through `new Date("2026-06-21")`, which is UTC midnight and
    // would render as 20 Jun for a reader east of the reporting zone.
    expect(formatDay("2026-06-21")).toBe("21 Jun");
    expect(formatDay("2026-01-05", true)).toBe("5 Jan 2026");
  });

  it("gives an unparseable key back rather than inventing a date", () => {
    expect(formatDay("not-a-day")).toBe("not-a-day");
  });
});

describe("formatInstant", () => {
  it("names the day in the zone the response was bucketed in", () => {
    // 05:00Z on 21 June is midnight in Chicago: the zone is what decides the date, so
    // a page that formatted in the reader's own zone would caption the wrong day.
    expect(formatInstant("2026-06-21T05:00:00Z", "America/Chicago")).toBe("21 Jun 2026");
    expect(formatInstant("2026-06-21T04:59:00Z", "America/Chicago")).toBe("20 Jun 2026");
  });

  it("says nothing for an absent instant", () => {
    expect(formatInstant(null, "UTC")).toBeNull();
  });
});

describe("formatBucket", () => {
  it("names a wide bucket as a span rather than as a day", () => {
    expect(formatBucket("2026-09-07", "week")).toBe("week of 7 Sep");
    expect(formatBucket("2026-09-07", "day")).toBe("7 Sep");
  });
});

describe("historyOf", () => {
  it("calls a project with no baseline empty rather than new", () => {
    // §9.1 and §9.2 are different sentences. "Nothing has happened" is a null
    // baseline; "we have nine days" is a short one.
    const empty = historyOf({ baseline_at: null }, NOW);
    expect(empty).toEqual({ depth: "none", days: null });
  });

  it("calls a project under the threshold thin", () => {
    const thin = historyOf({ baseline_at: "2026-09-10T00:00:00Z" }, NOW);
    expect(thin.depth).toBe("thin");
    expect(thin.days).toBe(8);
  });

  it("crosses into full history exactly at the threshold", () => {
    const at = new Date(NOW.getTime() - THIN_HISTORY_DAYS * 86_400_000);
    expect(historyOf({ baseline_at: at.toISOString() }, NOW).depth).toBe("full");
    const just = new Date(at.getTime() + 1000);
    expect(historyOf({ baseline_at: just.toISOString() }, NOW).depth).toBe("thin");
  });
});

describe("openLevel", () => {
  it("floors an impossible level and says how often it had to", () => {
    // §4.3: a store imported before creation times were floored opens its series at
    // −3. Drawing that is not an option and drawing it silently is worse.
    const level = openLevel([
      backlogPoint("2025-10-26", { open_count: -3 }),
      backlogPoint("2025-10-27", { open_count: 4 }),
    ]);
    expect(level.values).toEqual([0, 4]);
    expect(level.clamped).toBe(1);
    expect(clampWarning(level)).toContain("below zero in one bucket");
  });

  it("says nothing at all about a store that is fine", () => {
    const level = openLevel([backlogPoint("2026-06-21", { open_count: 12 })]);
    expect(level.clamped).toBe(0);
    expect(clampWarning(level)).toBeNull();
  });
});

describe("deltaWindow", () => {
  it("names the nominal window when coverage reaches all of it", () => {
    const data = response();
    expect(deltaWindow(data.range, { ...data.coverage, complete: true })).toBe("in 90 days");
  });

  it("names the date the window really starts when it does not", () => {
    // §9.3: the number is never attributed to a window it does not cover.
    const data = response();
    expect(deltaWindow(data.range, data.coverage)).toBe("since 21 Jun 2026");
  });
});

describe("summaryTiles", () => {
  const full = historyOf(response().coverage, NOW);
  const baselineOf = (data = response()) => deltaBaseline(data.range, data.coverage);

  it("gives every count a delta computed from a series in the same payload", () => {
    const tiles = summaryTiles(response(), full, baselineOf());
    expect(tiles.map((tile) => tile.key)).toEqual([
      "open",
      "created",
      "completed",
      "human",
      "external",
    ]);
    const open = tiles[0];
    expect(open?.count).toBe(149);
    // 10 opened less 1 closed over the two buckets inside native coverage. The 131
    // opened before it are on the chart and not in this number.
    expect(open?.delta).toEqual({ value: 9, direction: "up", tone: "bad", kind: "change" });
  });

  it("differences against native history, not against the backfilled floor (ac-3)", () => {
    // §19.1. The failure this replaces: a 90-day window over a store whose events were
    // reconstructed back to October 2025 reported *"149 open ▲ 145 more since 22 Jun"*
    // -- a delta four short of its own total, which is the total wearing an arrow.
    const tiles = summaryTiles(response(), full, baselineOf());
    for (const tile of tiles) {
      expect(
        tile.delta === null || Math.abs(tile.delta.value) !== tile.count,
        `${tile.key} reported a delta equal to its own count`,
      ).toBe(true);
    }
    expect(tiles[0]?.delta?.value).toBe(9);
    expect(tiles[0]?.count).toBe(149);
  });

  it("names the date it compares against while native history is younger than the range", () => {
    expect(baselineOf().words).toBe("since 7 Sep 2026");
    expect(baselineOf().day).toBe("2026-09-07");
  });

  it("names the range once native history is older than it", () => {
    const data = response();
    const baseline = deltaBaseline(data.range, {
      ...data.coverage,
      native_from: "2026-01-01T00:00:00Z",
    });
    expect(baseline.words).toBe("in 90 days");
    expect(baseline.day).toBe("2026-06-21");
  });

  it("refuses a comparison at all where nothing was recorded natively", () => {
    const data = response();
    const baseline = deltaBaseline(data.range, { ...data.coverage, native_from: null });
    expect(baseline.suppressed).toBe("nothing recorded natively yet");
    const tiles = summaryTiles(data, full, baseline);
    expect(tiles.every((tile) => tile.delta === null)).toBe(true);
    expect(tiles[0]?.count).toBe(149);
  });

  it("knows that up is bad for the backlog and good for completions", () => {
    // §8.2: red-up is bad for backlog and good for completed, so colour alone cannot
    // carry the meaning and the tone has to be decided here rather than by a class.
    const tiles = summaryTiles(response(), full, baselineOf());
    expect(tiles.find((tile) => tile.key === "completed")?.delta?.tone).toBe("good");
    expect(tiles.find((tile) => tile.key === "human")?.delta?.tone).toBe("bad");
  });

  it("counts a flow rather than differencing two levels where that is the truth", () => {
    const tiles = summaryTiles(response(), full, baselineOf());
    const completed = tiles.find((tile) => tile.key === "completed");
    expect(completed?.delta?.kind).toBe("flow");
    // The 60 completed in June are outside native coverage and are not in the flow.
    expect(completed?.delta?.value).toBe(4);
  });

  it("suppresses every delta, and no count, on a thin history", () => {
    // §9.2: trends are suppressed, values are not.
    const thin = historyOf({ baseline_at: "2026-09-12T00:00:00Z" }, NOW);
    const tiles = summaryTiles(response(), thin, baselineOf());
    expect(tiles.every((tile) => tile.delta === null)).toBe(true);
    expect(tiles.every((tile) => tile.suppressed?.includes("days of history"))).toBe(true);
    expect(tiles[0]?.count).toBe(149);
  });

  it("suppresses every delta on a project with no history, and still shows the counts", () => {
    const empty = response({ backlog: [], holders: [], throughput: [] });
    const tiles = summaryTiles(empty, historyOf({ baseline_at: null }, NOW), baselineOf(empty));
    expect(tiles.every((tile) => tile.delta === null)).toBe(true);
    expect(tiles[0]?.suppressed).toBe("no history yet");
    expect(tiles.map((tile) => tile.count)).toEqual([149, 453, 273, 29, 2]);
  });

  it("leads each count to the filter that shows it", () => {
    const tiles = summaryTiles(response(), full, baselineOf());
    expect(tiles.map((tile) => tile.status)).toEqual(["open", "all", "closed", "human", "external"]);
  });
});

describe("the readouts", () => {
  it("prints every value the backlog chart holds, because a phone has no hover", () => {
    expect(
      backlogReadout(backlogPoint("2026-09-18", { open_count: 149, opened: 8, closed: 6 }), "day"),
    ).toBe("18 Sep · 149 open · 8 opened · 6 closed");
  });

  it("names all three holder bands", () => {
    expect(holderReadout({ day: "2026-09-18", agent: 118, human: 29, external: 2 }, "day")).toBe(
      "18 Sep · 118 agent · 29 human · 2 external",
    );
  });
});

describe("the small words", () => {
  it("prints an em dash where there is no number, never a zero", () => {
    expect(days(null)).toBe("—");
    expect(days(3.14)).toBe("3.1d");
  });

  it("reads a stuck group as a sentence in the app's own vocabulary", () => {
    expect(
      stuckPhrase({
        ball: "human",
        ball_reason: "spec",
        tasks: 29,
        mean_days_held: 23.7,
        max_days_held: 34.2,
        oldest_task_id: "task-101",
      }),
    ).toBe("29 tasks waiting on you — spec · longest 34 days");
  });

  it("rounds an age to something a person would say", () => {
    expect(ageWords(0.4)).toBe("today");
    expect(ageWords(1.2)).toBe("1 day");
    expect(ageWords(50.92)).toBe("51 days");
  });
});
