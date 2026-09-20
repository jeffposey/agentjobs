import { describe, expect, it } from "vitest";

import type {
  AnalyticsCoverage,
  AnalyticsRange,
  FinishPoint,
  GatePoint,
  MachinePoint,
  RunPoint,
  ReviewPoint,
  SegmentPoint,
  StuckGroup,
} from "../api/types";
import {
  blankUnderSample,
  costReadout,
  counted,
  deltaBaseline,
  finishReadout,
  gateReadout,
  hours,
  localDayKey,
  minutes,
  orderStuck,
  reviewReadout,
  runReadout,
  seconds,
  segmentBlanks,
  segmentReadout,
  segmentStack,
  seriesCaption,
  sinceBaseline,
  startsLate,
  stepWords,
  stuckBand,
  stuckRowPhrase,
  throughputOnlyReadout,
  unknownBuckets,
  vocabulary,
  waitWords,
} from "./analyticsSecondSet";

/**
 * The arithmetic and the words of the second set (`docs/analytics-design.md` §18, §19).
 *
 * Nothing here renders. What is asserted is what a reader will act on -- which date a
 * delta is measured from, which bucket has no number, which band a stuck row sits in,
 * and the exact sentence each readout produces -- because those are the claims this
 * page can get wrong quietly. The drawing is measured in a browser instead.
 */

const RANGE: AnalyticsRange = {
  key: "30d",
  start: "2026-08-20T05:00:00Z",
  end: "2026-09-19T05:00:00Z",
  bucket: "day",
  throughput_bucket: "day",
  timezone: "America/Chicago",
};

const COVERAGE: AnalyticsCoverage = {
  baseline_at: "2025-10-25T19:00:00Z",
  baseline_kind: "backfilled",
  native_from: "2026-09-07T19:06:00Z",
  reconstructed_before: "2026-09-07T19:01:00Z",
  events: {},
  complete: false,
  note: "History from 25 Oct 2025.",
};

describe("the units", () => {
  it("spells a duration in the size a person would say it", () => {
    expect(hours(0.25)).toBe("15 min");
    expect(hours(4.75)).toBe("4.8 h");
    expect(hours(300)).toBe("12.5 d");
    expect(minutes(0.5)).toBe("30 s");
    expect(minutes(4.8)).toBe("4.8 min");
    expect(seconds(359)).toBe("6.0 min");
    expect(seconds(2.2)).toBe("2.2 s");
  });

  it("prints an em dash where there is no number, never a zero", () => {
    // §9.3, applied to every unit: a null percentile and a zero one are different
    // facts, and a page that printed "0 h" for the first would be inventing a value.
    expect(hours(null)).toBe("—");
    expect(minutes(undefined)).toBe("—");
    expect(seconds(Number.NaN)).toBe("—");
    // A real zero is still a zero, and says so.
    expect(hours(0)).toBe("0");
  });

  it("pluralises a count with its noun", () => {
    expect(counted(1, "task")).toBe("1 task");
    expect(counted(3, "task")).toBe("3 tasks");
    expect(counted(2, "finish", "finishes")).toBe("2 finishes");
  });

  it("reads a small vocabulary largest first, and says nothing about an empty one", () => {
    expect(vocabulary({ manual: 9, child: 3, auto: 1 })).toBe("manual 9, child 3, auto 1");
    expect(vocabulary({ manual: 0 })).toBeNull();
    expect(vocabulary(undefined)).toBeNull();
    expect(stepWords({ gate: 253, merge: 1.2 })).toBe("gate 4.2 min, merge 1.2 s");
  });

  it("says a wait in the size that is actionable", () => {
    expect(waitWords(0.3)).toBe("18 min");
    expect(waitWords(5.4)).toBe("5.4 h");
    expect(waitWords(52)).toBe("2 days");
  });
});

describe("localDayKey", () => {
  it("answers which day an instant is, in the zone the response was bucketed in", () => {
    // 05:00Z is midnight in Chicago. A page that converted in the reader's own zone
    // would compare a bucket key against the wrong day and silently shift the window.
    expect(localDayKey("2026-09-08T05:00:00Z", "America/Chicago")).toBe("2026-09-08");
    expect(localDayKey("2026-09-08T04:59:00Z", "America/Chicago")).toBe("2026-09-07");
  });

  it("says nothing for an absent or unparseable instant", () => {
    expect(localDayKey(null, "UTC")).toBeNull();
    expect(localDayKey("not-an-instant", "UTC")).toBeNull();
  });
});

describe("deltaBaseline (§19.1)", () => {
  it("compares against native history where it begins inside the range", () => {
    const baseline = deltaBaseline(RANGE, COVERAGE);
    expect(baseline.day).toBe("2026-09-07");
    expect(baseline.words).toBe("since 7 Sep 2026");
    expect(baseline.suppressed).toBeNull();
  });

  it("compares against the range once native history is older than it", () => {
    const baseline = deltaBaseline(RANGE, { ...COVERAGE, native_from: "2026-01-01T00:00:00Z" });
    expect(baseline.day).toBe("2026-08-20");
    expect(baseline.words).toBe("in 30 days");
  });

  it("never uses the backfilled floor, whatever it says", () => {
    // The failure: `baseline_at` is October 2025 here, and differencing a level
    // against it is what produced *"149 open ▲ 145 more since 22 Jun"*.
    const baseline = deltaBaseline(RANGE, COVERAGE);
    expect(baseline.day).not.toBe("2025-10-25");
    expect(baseline.words).not.toContain("2025");
  });

  it("refuses a comparison where nothing was recorded natively at all", () => {
    const baseline = deltaBaseline(RANGE, { ...COVERAGE, native_from: null });
    expect(baseline.day).toBeNull();
    expect(baseline.suppressed).toBe("nothing recorded natively yet");
  });

  it("names each range in its own words", () => {
    for (const [key, words] of [
      ["30d", "in 30 days"],
      ["90d", "in 90 days"],
      ["12m", "in 12 months"],
      ["all", "over all history"],
    ] as const) {
      const baseline = deltaBaseline(
        { ...RANGE, key },
        { ...COVERAGE, native_from: "2026-01-01T00:00:00Z" },
      );
      expect(baseline.words).toBe(words);
    }
  });
});

describe("sinceBaseline", () => {
  it("keeps only the buckets at or after the baseline", () => {
    const points = [{ day: "2026-09-06" }, { day: "2026-09-07" }, { day: "2026-09-08" }];
    const kept = sinceBaseline(points, (point) => point.day, deltaBaseline(RANGE, COVERAGE));
    expect(kept.map((point) => point.day)).toEqual(["2026-09-07", "2026-09-08"]);
  });

  it("keeps nothing where no comparison may be made", () => {
    const points = [{ day: "2026-09-08" }];
    const baseline = deltaBaseline(RANGE, { ...COVERAGE, native_from: null });
    expect(sinceBaseline(points, (point) => point.day, baseline)).toEqual([]);
  });
});

describe("per-series coverage (§21.1)", () => {
  it("renders the sentence the API wrote rather than deriving one", () => {
    expect(seriesCaption({ note: "Finishes are recorded from 23 Aug 2026." })).toBe(
      "Finishes are recorded from 23 Aug 2026.",
    );
    expect(seriesCaption({ note: null })).toBeNull();
    expect(seriesCaption(undefined)).toBeNull();
  });

  it("knows a series that starts inside the window from one that covers it", () => {
    expect(startsLate({ recorded_from: "2026-08-23T00:00:00Z", complete: false })).toBe(true);
    expect(startsLate({ recorded_from: "2025-01-01T00:00:00Z", complete: true })).toBe(false);
    expect(startsLate({ recorded_from: null, complete: false })).toBe(false);
  });
});

describe("blanking an under-sampled bucket (§9.3)", () => {
  const points = [
    { value: 4.8, sample: 7 },
    { value: 40, sample: 2 },
    { value: null, sample: 9 },
  ];

  it("blanks a bucket the sample cannot support, and one with no value at all", () => {
    const series = blankUnderSample(
      points,
      (point) => point.value,
      (point) => point.sample,
    );
    expect(series).toEqual([4.8, null, null]);
    expect(unknownBuckets(series)).toEqual([false, true, true]);
  });
});

function segmentPoint(over: Partial<SegmentPoint> = {}): SegmentPoint {
  return {
    bucket: "2026-09-07",
    sample: 22,
    excluded: 0,
    unreviewed: 0,
    first_review_sample: 0,
    estimated: false,
    among: {},
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
    ...over,
  };
}

describe("the segment stack (S1)", () => {
  it("stacks the five medians in §17.1's order", () => {
    const stack = segmentStack([segmentPoint()]);
    expect(stack.map((one) => one[0])).toEqual([1.1, 0.6, 0, 0, 0]);
  });

  it("leaves an under-sampled bucket at no height and marks it blank", () => {
    const points = [segmentPoint(), segmentPoint({ bucket: "2026-09-14", sample: 2 })];
    expect(segmentStack(points).map((one) => one[1])).toEqual([0, 0, 0, 0, 0]);
    expect(segmentBlanks(points)).toEqual([false, true]);
  });

  it("reads a bucket as the five medians, the total and the sample", () => {
    const readout = segmentReadout(segmentPoint({ excluded: 1, first_review_sample: 9, first_review_p50_hours: 0.8, first_review_p90_hours: 4 }), "week");
    expect(readout).toContain("week of 7 Sep");
    expect(readout).toContain("queue 1.1 h/4.0 d");
    expect(readout).toContain("work 36 min/1.5 h");
    expect(readout).toContain("total p50 4.8 h · p90 12.5 d");
    expect(readout).toContain("22 tasks");
    expect(readout).toContain("to first review 48 min/4.0 h over 9 tasks");
    expect(readout).toContain("1 excluded (imported close)");
  });

  it("says a bucket is not measured rather than printing five noughts", () => {
    const readout = segmentReadout(segmentPoint({ sample: 2 }), "week");
    expect(readout).toBe("week of 7 Sep · not measured — 2 tasks completed, 3 needed");
  });

  it("says so where there is no bucket at all", () => {
    expect(segmentReadout(undefined, "week")).toBe("No buckets in this window.");
  });
});

describe("the throughput readout, with cycle time gone (§19.2)", () => {
  const point = {
    bucket: "2026-09-18",
    tasks_completed: 3,
    completion_events: 3,
    cancelled: 0,
    reopened: 0,
    estimated: false,
  };

  it("mentions completion events only when they disagree with the task count", () => {
    expect(throughputOnlyReadout(point, "day")).not.toContain("completion events");
    expect(throughputOnlyReadout({ ...point, completion_events: 4, reopened: 1 }, "day")).toContain(
      "4 completion events — a task was reopened",
    );
  });

  it("says nothing about a median, because it no longer has one", () => {
    expect(throughputOnlyReadout(point, "day")).toBe("18 Sep · 3 completed");
  });
});

describe("the finish and gate readouts (F1–F4, G1–G3)", () => {
  const finish: FinishPoint = {
    bucket: "2026-09-07",
    finished: 7,
    escalated: 2,
    declined: 0,
    interrupted: 1,
    reasons: { gate_failed: 2 },
    duration_p50_min: 4.8,
    duration_p90_min: 8.3,
    sample: 7,
    steps_p50_s: { gate: 253 },
    runway_waited: 2,
    runway_p90_s: 359,
    estimated: false,
  };
  const gate: GatePoint = {
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
  };

  it("names the outcomes, the escalation reasons and the runway wait", () => {
    const readout = finishReadout(finish, "week");
    expect(readout).toContain("10 started");
    expect(readout).toContain("7 finished · 2 escalated · 0 declined · 1 interrupted");
    expect(readout).toContain("escalations: gate_failed 2");
    expect(readout).toContain("finished p50 4.8 min · p90 8.3 min");
    expect(readout).toContain("steps: gate 4.2 min");
    expect(readout).toContain("2 waited for the runway, p90 6.0 min");
  });

  it("says nothing about a runway nobody waited for", () => {
    // §18.3's F4: a value that is zero nine weeks in ten is not a chart and is not a
    // clause either.
    expect(finishReadout({ ...finish, runway_waited: 0 }, "week")).not.toContain("runway");
  });

  it("gives the green rate from the other side, with the stage split", () => {
    const readout = gateReadout(gate, "week");
    expect(readout).toContain("9 full gates");
    expect(readout).toContain("7 green (78%)");
    expect(readout).toContain("green p50 4.0 min · p90 6.1 min");
    expect(readout).toContain("stages: pytest 2.1 min, e2e 1.8 min");
    expect(readout).toContain("red at: pytest 2");
    expect(readout).toContain("from finish 7, run 2");
  });
});

describe("the run readout (R-1 to R-6)", () => {
  const run: RunPoint = {
    bucket: "2026-09-16",
    runs: 12,
    triggers: { manual: 9, child: 3 },
    agent_hours: 6.4,
    outcomes: { completed: 10, interrupted: 2 },
    in_flight: 1,
    duration_p50_min: 27,
    duration_p90_min: 97,
    sample: 12,
    estimated: false,
  };
  const machine: MachinePoint = {
    bucket: "2026-09-16",
    admitted: 12,
    start_latency_p50_s: 2.2,
    start_latency_p90_s: 3.1,
    queued: 2,
    queue_wait_p50_s: 90,
    queue_wait_p90_s: 140,
    paused_run_hours: 6.9,
    paused_waiters: 5,
  };

  it("carries the counts, the hours, the queue wait and the paused hours", () => {
    const readout = runReadout(run, machine, "day");
    expect(readout).toContain("12 runs · 6.4 agent-hours");
    expect(readout).toContain("triggers: manual 9, child 3");
    expect(readout).toContain("1 still in the air");
    expect(readout).toContain("2 queued, waited p50 1.5 min");
    expect(readout).toContain("6.9 run-hours paused on a usage limit across 5 waiters");
  });

  it("falls back to the start latency where nothing had to queue", () => {
    // §18.5's R-5: until a dispatch queues, the number that exists is the launcher's
    // own latency, and the readout says which of the two it is showing.
    const readout = runReadout(run, { ...machine, queued: 0, paused_run_hours: 0 }, "day");
    expect(readout).toContain("start latency p50 2.2 s");
    expect(readout).not.toContain("queued");
    expect(readout).not.toContain("paused");
  });

  it("reads without a machine bucket at all, because that series starts later", () => {
    expect(runReadout(run, undefined, "day")).toContain("12 runs");
  });
});

describe("the review readout (R2, R3, Q-2)", () => {
  const point: ReviewPoint = {
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
  };

  it("carries both sides of how long a person took", () => {
    const readout = reviewReadout(point, "week");
    expect(readout).toContain("34 reviews answered · waited p50 4 min · p90 5.1 h");
    expect(readout).toContain("14 of 24 approved first time");
    expect(readout).toContain("5 questions asked, 4 answered p50 16 min · p90 30.0 h");
    expect(readout).toContain("1 still open");
  });

  it("says a week answered nothing rather than printing a zero wait", () => {
    const quiet = reviewReadout({ ...point, exits: 0, approvals: 0, questions: 0 }, "week");
    expect(quiet).toContain("no reviews answered");
    expect(quiet).not.toContain("p50");
  });
});

describe("the cost readout (S4, F5, G4)", () => {
  it("names the owner's three numbers", () => {
    const readout = costReadout(
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
      },
      "week",
    );
    expect(readout).toContain("22 tasks completed");
    expect(readout).toContain("1.4 runs each, most often 1");
    expect(readout).toContain("1.3 finishes each");
    expect(readout).toContain("gate p50 4.4 min · p90 6.9 min");
    expect(readout).toContain("2 with no gate at all");
  });

  it("says a bucket completed nothing rather than dividing by it", () => {
    const readout = costReadout(
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
      },
      "week",
    );
    expect(readout).toBe("week of 14 Sep · nothing completed");
  });
});

describe("stuck, reordered (§19.3)", () => {
  const group = (ball: string, reason: string, tasks: number): StuckGroup => ({
    ball,
    ball_reason: reason,
    tasks,
    mean_days_held: 1,
    max_days_held: 40.5,
    oldest_task_id: `task-${tasks}`,
  });

  it("bands a row by who is being waited on", () => {
    expect(stuckBand(group("human", "review", 1))).toBe("human");
    expect(stuckBand(group("external", "dependency", 1))).toBe("blocked");
    expect(stuckBand(group("agent", "work", 1))).toBe("agent");
    expect(stuckBand(group("agent", "available", 1))).toBe("queue");
  });

  it("puts the queue last however many tasks are in it", () => {
    // The failure: the API sorts by count descending, and *96 ready, unclaimed* was
    // therefore the headline of a panel called "Stuck".
    const ordered = orderStuck([
      group("agent", "available", 96),
      group("agent", "work", 3),
      group("external", "dependency", 2),
      group("human", "spec", 29),
    ]);
    expect(ordered.map((row) => row.band)).toEqual(["human", "blocked", "agent", "queue"]);
  });

  it("still breaks a tie inside a band by count", () => {
    const ordered = orderStuck([
      group("human", "decision", 2),
      group("human", "review", 11),
    ]);
    expect(ordered.map((row) => row.group.ball_reason)).toEqual(["review", "decision"]);
  });

  it("names the queue as the queue, and everything else as §8.6 worded it", () => {
    const [queue] = orderStuck([group("agent", "available", 96)]);
    expect(queue && stuckRowPhrase(queue)).toBe("96 tasks ready, unclaimed · longest 41 days");
    const [human] = orderStuck([group("human", "spec", 29)]);
    expect(human && stuckRowPhrase(human)).toBe("29 tasks waiting on you — spec · longest 41 days");
  });
});
