/**
 * What the analytics response's *second set* means, as pure functions.
 *
 * The split from `analyticsSeries.ts` is by pass rather than by kind: that file is
 * task-373's four panels and the three states of `docs/analytics-design.md` §9, this
 * one is §18's metric catalogue -- segments, finishes, gates, runs, review and
 * questions. Both are arithmetic and words with no DOM in them, for the reason §10.1
 * gives: jsdom can check all of this and none of the drawing, so the honest half is
 * kept where a test can reach it.
 *
 * Three rules bind everything below.
 *
 * - **Never substitute zero for unknown** (§9.3). A percentile over two tasks is not a
 *   number, a week before a source existed has no value, and both render as a blank
 *   bucket and a sentence rather than as a bar of height nought.
 * - **The readout is the p90** (§19.5). The first page drew a p50-to-p90 band and the
 *   owner could not read it; the band is gone and the tapped bucket's readout carries
 *   both percentiles instead, which is what §10.4 already made the primary gesture.
 * - **A unit is in the name.** Hours for task-scale durations, minutes for finishes and
 *   gates, seconds for latencies -- §21.2's rule, carried through to the words a reader
 *   sees so no number on this page is ambiguous about what it counts.
 */

import type {
  AnalyticsCoverage,
  AnalyticsRange,
  CostPerTaskPoint,
  FinishPoint,
  GatePoint,
  MachinePoint,
  RunPoint,
  SegmentPoint,
  SeriesCoverage,
  StuckGroup,
  ReviewPoint,
  ThroughputPoint,
} from "../api/types";
import { PERCENTILE_MIN_SAMPLE, formatBucket, formatInstant, stuckPhrase } from "./analyticsSeries";

/**
 * The range the page opens on (§19.1), which moved from 90d to 30d.
 *
 * Not because thirty days sits inside native coverage -- nothing offered does yet --
 * but because the page is now mostly machine series at day grain, and thirty bars fit
 * a phone where ninety do not. §10.4's rule is aggregate rather than scroll, and a
 * week-grain runs-per-day chart would answer a different question. It is one constant
 * shared with the API's own `DEFAULT_RANGE`, so the page and the endpoint cannot open
 * on different windows.
 */
export const DEFAULT_ANALYTICS_RANGE = "30d";

/** A number of hours as a reader says it, or an em dash where there is no number. */
export function hours(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  if (value === 0) return "0";
  if (value < 1) return `${Math.round(value * 60)} min`;
  if (value >= 48) return `${(value / 24).toFixed(1)} d`;
  return `${value.toFixed(1)} h`;
}

/** A number of minutes, in the same shape. Finishes and gates are measured in these. */
export function minutes(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  if (value === 0) return "0";
  if (value < 1) return `${Math.round(value * 60)} s`;
  return `${value.toFixed(1)} min`;
}

/** A number of seconds. Latencies are in these, and they are usually single digits. */
export function seconds(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  if (value >= 60) return `${(value / 60).toFixed(1)} min`;
  return `${value.toFixed(1)} s`;
}

/** A plain count with its noun, pluralised. Used everywhere a sample is stated. */
export function counted(value: number, singular: string, plural = `${singular}s`): string {
  return `${value} ${value === 1 ? singular : plural}`;
}

/** A `{key: count}` vocabulary as words, largest first, or null when it is empty. */
export function vocabulary(
  table: Record<string, number> | undefined,
  limit = 3,
): string | null {
  const entries = Object.entries(table ?? {}).filter(([, count]) => count > 0);
  if (entries.length === 0) return null;
  entries.sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
  return entries
    .slice(0, limit)
    .map(([key, count]) => `${key} ${count}`)
    .join(", ");
}

/** A `{key: seconds}` vocabulary as words, slowest first. F3's and G2's tap treatment. */
export function stepWords(table: Record<string, number> | undefined, limit = 4): string | null {
  const entries = Object.entries(table ?? {}).filter(([, value]) => value > 0);
  if (entries.length === 0) return null;
  entries.sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
  return entries
    .slice(0, limit)
    .map(([key, value]) => `${key} ${seconds(value)}`)
    .join(", ");
}

/**
 * The caption a series carries about what it can honestly claim (§21.1).
 *
 * The sentence is written by the API, beside the rows it is a statement about, so the
 * page never derives one from three nullable fields and never gets to be optimistic
 * about a source it cannot see. A null note means the series covers the window and
 * there is nothing to say -- which is a state, not a missing caption.
 */
export function seriesCaption(coverage: SeriesCoverage | undefined): string | null {
  return coverage?.note ?? null;
}

/**
 * Whether a series is shorter than the window because its source is younger (§9.3).
 *
 * The page uses it to decide whether to say *where the series starts* rather than to
 * pad the series back to the range with zeros. Padding is the failure §9.3 forbids by
 * name: a bar of height nought and a week nobody recorded look identical.
 */
export function startsLate(coverage: SeriesCoverage | undefined): boolean {
  return Boolean(coverage?.recorded_from) && coverage?.complete !== true;
}

/**
 * A percentile series with every bucket the sample cannot support left blank.
 *
 * `null` rather than zero, in the array the chart draws from, so the blank is carried
 * all the way to the geometry rather than being reconstructed from the sample there.
 */
export function blankUnderSample<T>(
  points: readonly T[],
  value: (point: T) => number | null | undefined,
  sample: (point: T) => number,
  minimum = PERCENTILE_MIN_SAMPLE,
): Array<number | null> {
  return points.map((point) => {
    if (sample(point) < minimum) return null;
    const found = value(point);
    return found === null || found === undefined || !Number.isFinite(found) ? null : found;
  });
}

/** Which buckets of a percentile series have no number, for the blank-span treatment. */
export function unknownBuckets(values: ReadonlyArray<number | null>): boolean[] {
  return values.map((value) => value === null);
}

// ---------------------------------------------------------------------------
// The summary row's delta baseline (§19.1)
// ---------------------------------------------------------------------------

/**
 * A `YYYY-MM-DD` day key for an instant, in the zone the whole response was bucketed
 * in -- the one conversion this page is allowed to do.
 *
 * Through a fixed `en-CA` format for the same reason {@link formatInstant} does it:
 * the answer wanted is *which day it is over there*, and every other locale spells
 * that differently while a few reorder it.
 */
export function localDayKey(iso: string | null | undefined, timeZone: string): string | null {
  if (!iso) return null;
  const instant = new Date(iso);
  if (Number.isNaN(instant.getTime())) return null;
  try {
    return new Intl.DateTimeFormat("en-CA", {
      timeZone,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    }).format(instant);
  } catch {
    return instant.toISOString().slice(0, 10);
  }
}

/** What the summary row compares against, and the words the tile says it in. */
export interface DeltaBaseline {
  /** The day the comparison starts, as a bucket key, or null when there is none. */
  day: string | null;
  /** *"in 30 days"* where native history covers the window, *"since 7 Sep"* where not. */
  words: string;
  /** Why no comparison may be made at all. Null when one may. */
  suppressed: string | null;
}

/**
 * §19.1: the baseline is `max(range.start, coverage.native_from)`, and the tile names
 * the date it compares against.
 *
 * **Never `baseline_at`.** That is the backfilled floor of October 2025, and comparing
 * a count against it is what produced *"149 open ▲ 145 more since 22 Jun"* -- a delta
 * within four of its own total, which is a restatement of the total wearing an arrow.
 * The backfill knows when a task was created and not when the ball moved, so a level
 * differenced against it is a difference against nothing.
 *
 * A project with no native history at all gets no delta and a sentence saying so,
 * which is §8.2's suppression rule amended to fire on `native_from`.
 */
export function deltaBaseline(
  range: AnalyticsRange,
  coverage: AnalyticsCoverage,
): DeltaBaseline {
  const nativeDay = localDayKey(coverage.native_from, range.timezone);
  const startDay = localDayKey(range.start, range.timezone);
  if (!nativeDay) {
    return {
      day: null,
      words: "",
      suppressed: "nothing recorded natively yet",
    };
  }
  if (!startDay || nativeDay <= startDay) {
    const spans: Record<string, string> = {
      "30d": "in 30 days",
      "90d": "in 90 days",
      "12m": "in 12 months",
      all: "over all history",
    };
    return { day: startDay, words: spans[range.key] ?? "over the window shown", suppressed: null };
  }
  const named = formatInstant(coverage.native_from, range.timezone);
  return { day: nativeDay, words: named ? `since ${named}` : "since native history began", suppressed: null };
}

/** The buckets of a series at or after the baseline, which is what a delta is summed over. */
export function sinceBaseline<T>(
  points: readonly T[],
  key: (point: T) => string,
  baseline: DeltaBaseline,
): T[] {
  if (!baseline.day) return [];
  const from = baseline.day;
  return points.filter((point) => key(point) >= from);
}

// ---------------------------------------------------------------------------
// Stuck, reordered (§19.3)
// ---------------------------------------------------------------------------

/** Which band a stuck row sits in. The order of this list is the order on the page. */
export type StuckBand = "human" | "blocked" | "agent" | "queue";

export interface StuckRow {
  band: StuckBand;
  group: StuckGroup;
}

const BAND_ORDER: Record<StuckBand, number> = { human: 0, blocked: 1, agent: 2, queue: 3 };

/** The heading each band carries, which is the whole of §19.3's reordering. */
export const STUCK_BAND_LABEL: Record<StuckBand, string> = {
  human: "Waiting on you",
  blocked: "Blocked",
  agent: "With an agent",
  queue: "The queue",
};

/** Which band a `(ball, ball_reason)` pair belongs to. */
export function stuckBand(group: StuckGroup): StuckBand {
  if (group.ball === "human") return "human";
  if (group.ball === "external") return "blocked";
  if (group.ball === "agent" && group.ball_reason === "available") return "queue";
  return "agent";
}

/**
 * §19.3: rows ordered by **who is being waited on**, not by count.
 *
 * The first page sorted by count descending, so *29 tasks ready and unclaimed* was the
 * headline of a panel called "Stuck" -- and a backlog waiting its turn is not stuck, it
 * is the queue. It goes last here and says what it is. Inside a band the count still
 * decides, because once the bands are separated the count is a useful tie-break rather
 * than a claim about urgency.
 */
export function orderStuck(groups: readonly StuckGroup[]): StuckRow[] {
  return groups
    .map((group) => ({ band: stuckBand(group), group }))
    .sort(
      (a, b) =>
        BAND_ORDER[a.band] - BAND_ORDER[b.band] ||
        b.group.tasks - a.group.tasks ||
        a.group.ball_reason.localeCompare(b.group.ball_reason),
    );
}

/**
 * One stuck row in words. The queue is named as the queue and nothing else is.
 *
 * §19.3's last line: *"the backlog waiting its turn, and it says so"*. Every other row
 * keeps §8.6's wording, because those rows really are work that has stopped.
 */
export function stuckRowPhrase(row: StuckRow): string {
  const { group } = row;
  if (row.band !== "queue") return stuckPhrase(group);
  return (
    `${counted(group.tasks, "task")} ready, unclaimed · ` +
    `longest ${Math.round(group.max_days_held)} days`
  );
}

// ---------------------------------------------------------------------------
// Readouts (§18, one per panel)
// ---------------------------------------------------------------------------

/** The five segments of a task's life, bottom of the stack first (§17.1). */
export const SEGMENTS = ["queue", "work", "waiting", "review", "finish"] as const;
export type Segment = (typeof SEGMENTS)[number];

const SEGMENT_P50: Record<Segment, keyof SegmentPoint> = {
  queue: "queue_p50_hours",
  work: "work_p50_hours",
  waiting: "waiting_p50_hours",
  review: "review_p50_hours",
  finish: "finish_p50_hours",
};

const SEGMENT_P90: Record<Segment, keyof SegmentPoint> = {
  queue: "queue_p90_hours",
  work: "work_p90_hours",
  waiting: "waiting_p90_hours",
  review: "review_p90_hours",
  finish: "finish_p90_hours",
};

/** One segment's median for a bucket, or null where the sample cannot support one. */
export function segmentValue(point: SegmentPoint, segment: Segment, percentile: 50 | 90): number | null {
  const field = percentile === 50 ? SEGMENT_P50[segment] : SEGMENT_P90[segment];
  const value = point[field];
  if (typeof value !== "number" || !Number.isFinite(value)) return null;
  return value;
}

/** The five medians of a bucket as a stack, with an under-sampled bucket left at zero
 * height and marked blank by {@link segmentBlanks} rather than drawn as five noughts. */
export function segmentStack(points: readonly SegmentPoint[]): number[][] {
  return SEGMENTS.map((segment) =>
    points.map((point) =>
      point.sample < PERCENTILE_MIN_SAMPLE ? 0 : (segmentValue(point, segment, 50) ?? 0),
    ),
  );
}

/** Which buckets of the segment stack have no number at all (§18.1's minimum sample). */
export function segmentBlanks(points: readonly SegmentPoint[]): boolean[] {
  return points.map((point) => point.sample < PERCENTILE_MIN_SAMPLE);
}

/**
 * S1's readout: the five medians, the total, and the sample (§19.2).
 *
 * The p90s are here rather than on the chart because §19.5 replaced the band with the
 * tap: this line always describes the selected bucket, so a tap *is* how a reader
 * reaches the 90th percentile. The caption says once that the stack's height is the sum
 * of five medians and not the median total -- S2 supplies that, in this same line.
 */
export function segmentReadout(point: SegmentPoint | undefined, bucket: AnalyticsRange["bucket"]): string {
  if (!point) return "No buckets in this window.";
  const head = formatBucket(point.bucket, bucket);
  if (point.sample < PERCENTILE_MIN_SAMPLE) {
    return `${head} · not measured — ${counted(point.sample, "task")} completed, ${PERCENTILE_MIN_SAMPLE} needed`;
  }
  const parts = SEGMENTS.map(
    (segment) =>
      `${segment} ${hours(segmentValue(point, segment, 50))}/${hours(segmentValue(point, segment, 90))}`,
  );
  const total = `total p50 ${hours(point.total_p50_hours)} · p90 ${hours(point.total_p90_hours)}`;
  const firstReview = point.first_review_sample ?? 0;
  const review =
    firstReview > 0
      ? ` · to first review ${hours(point.first_review_p50_hours)}/${hours(point.first_review_p90_hours)} over ${counted(firstReview, "task")}`
      : "";
  const excluded = point.excluded > 0 ? ` · ${point.excluded} excluded (imported close)` : "";
  return `${head} · ${parts.join(" · ")} · ${total} · ${counted(point.sample, "task")}${review}${excluded}`;
}

/** T1's readout, now that cycle time has its own panel (§19.2). */
export function throughputOnlyReadout(
  point: ThroughputPoint | undefined,
  bucket: AnalyticsRange["bucket"],
): string {
  if (!point) return "No buckets in this window.";
  const parts = [formatBucket(point.bucket, bucket), `${point.tasks_completed} completed`];
  if (point.cancelled > 0) parts.push(`${point.cancelled} cancelled`);
  if (point.completion_events !== point.tasks_completed) {
    parts.push(`${point.completion_events} completion events — a task was reopened`);
  }
  if ((point.reopened ?? 0) > 0) parts.push(`${counted(point.reopened ?? 0, "task")} reopened`);
  return parts.join(" · ");
}

/** F1 to F4 in one line, with the escalation reasons and the runway wait §18.3 asks for. */
export function finishReadout(point: FinishPoint | undefined, bucket: AnalyticsRange["bucket"]): string {
  if (!point) return "No buckets in this window.";
  const total =
    (point.finished ?? 0) + (point.escalated ?? 0) + (point.declined ?? 0) + (point.interrupted ?? 0);
  const parts = [formatBucket(point.bucket, bucket), `${total} started`];
  parts.push(
    `${point.finished ?? 0} finished · ${point.escalated ?? 0} escalated · ${point.declined ?? 0} declined · ${point.interrupted ?? 0} interrupted`,
  );
  const reasons = vocabulary(point.reasons);
  if (reasons) parts.push(`escalations: ${reasons}`);
  if ((point.sample ?? 0) > 0) {
    parts.push(`finished p50 ${minutes(point.duration_p50_min)} · p90 ${minutes(point.duration_p90_min)}`);
  }
  const steps = stepWords(point.steps_p50_s);
  if (steps) parts.push(`steps: ${steps}`);
  if ((point.runway_waited ?? 0) > 0) {
    parts.push(
      `${point.runway_waited} waited for the runway, p90 ${seconds(point.runway_p90_s)}`,
    );
  }
  return parts.join(" · ");
}

/** G1 to G3: duration, the stage split, and the green rate from the other side. */
export function gateReadout(point: GatePoint | undefined, bucket: AnalyticsRange["bucket"]): string {
  if (!point) return "No buckets in this window.";
  const parts = [formatBucket(point.bucket, bucket), `${counted(point.full ?? 0, "full gate")}`];
  if ((point.full ?? 0) > 0) {
    parts.push(`${point.passed ?? 0} green (${Math.round(((point.passed ?? 0) / (point.full ?? 1)) * 100)}%)`);
  }
  if ((point.sample ?? 0) > 0) {
    parts.push(`green p50 ${minutes(point.duration_p50_min)} · p90 ${minutes(point.duration_p90_min)}`);
  }
  const stages = stepWords(point.stages_p50_s);
  if (stages) parts.push(`stages: ${stages}`);
  const failed = vocabulary(point.failed_stages);
  if (failed) parts.push(`red at: ${failed}`);
  const origins = vocabulary(point.origins);
  if (origins) parts.push(`from ${origins}`);
  return parts.join(" · ");
}

/** R-1 to R-4, plus R-5 and R-6 from the machine series where the bucket has one. */
export function runReadout(
  point: RunPoint | undefined,
  machine: MachinePoint | undefined,
  bucket: AnalyticsRange["bucket"],
): string {
  if (!point) return "No buckets in this window.";
  const parts = [
    formatBucket(point.bucket, bucket),
    counted(point.runs ?? 0, "run"),
    `${(point.agent_hours ?? 0).toFixed(1)} agent-hours`,
  ];
  const triggers = vocabulary(point.triggers);
  if (triggers) parts.push(`triggers: ${triggers}`);
  const outcomes = vocabulary(point.outcomes, 4);
  if (outcomes) parts.push(`outcomes: ${outcomes}`);
  if ((point.in_flight ?? 0) > 0) parts.push(`${point.in_flight} still in the air`);
  if ((point.sample ?? 0) > 0) {
    parts.push(`duration p50 ${minutes(point.duration_p50_min)} · p90 ${minutes(point.duration_p90_min)}`);
  }
  if (machine) {
    if ((machine.queued ?? 0) > 0) {
      parts.push(`${machine.queued} queued, waited p50 ${seconds(machine.queue_wait_p50_s)}`);
    } else if ((machine.admitted ?? 0) > 0) {
      parts.push(`start latency p50 ${seconds(machine.start_latency_p50_s)}`);
    }
    if ((machine.paused_run_hours ?? 0) > 0) {
      parts.push(
        `${(machine.paused_run_hours ?? 0).toFixed(1)} run-hours paused on a usage limit across ${counted(machine.paused_waiters ?? 0, "waiter")}`,
      );
    }
  }
  return parts.join(" · ");
}

/** R2, R3, Q-2 in one line: how long a person took, from both sides. */
export function reviewReadout(point: ReviewPoint | undefined, bucket: AnalyticsRange["bucket"]): string {
  if (!point) return "No buckets in this window.";
  const parts = [formatBucket(point.bucket, bucket)];
  if ((point.exits ?? 0) > 0) {
    parts.push(
      `${counted(point.exits ?? 0, "review", "reviews")} answered · waited p50 ${hours(point.wait_p50_hours)} · p90 ${hours(point.wait_p90_hours)}`,
    );
  } else {
    parts.push("no reviews answered");
  }
  if ((point.approvals ?? 0) > 0) {
    parts.push(
      `${point.first_time_approvals ?? 0} of ${point.approvals} approved first time`,
    );
  }
  if ((point.questions ?? 0) > 0) {
    const unanswered = (point.questions ?? 0) - (point.answered ?? 0);
    parts.push(
      `${counted(point.questions ?? 0, "question")} asked, ${point.answered ?? 0} answered p50 ${hours(point.answer_p50_hours)} · p90 ${hours(point.answer_p90_hours)}` +
        (unanswered > 0 ? `, ${unanswered} still open` : ""),
    );
  }
  return parts.join(" · ");
}

/** S4, F5 and G4: what one completed task cost, in runs, finishes and gate minutes. */
export function costReadout(
  point: CostPerTaskPoint | undefined,
  bucket: AnalyticsRange["bucket"],
): string {
  if (!point) return "No buckets in this window.";
  if (point.sample === 0) return `${formatBucket(point.bucket, bucket)} · nothing completed`;
  const parts = [formatBucket(point.bucket, bucket), `${counted(point.sample, "task")} completed`];
  if (point.runs_mean !== null && point.runs_mean !== undefined) {
    const mode =
      point.runs_mode !== null && point.runs_mode !== undefined
        ? `, most often ${point.runs_mode}`
        : "";
    parts.push(`${point.runs_mean.toFixed(1)} runs each${mode}`);
  }
  if (point.finishes_mean !== null && point.finishes_mean !== undefined) {
    parts.push(`${point.finishes_mean.toFixed(1)} finishes each`);
  }
  if (point.gate_minutes_p50 !== null && point.gate_minutes_p50 !== undefined) {
    parts.push(`gate p50 ${minutes(point.gate_minutes_p50)} · p90 ${minutes(point.gate_minutes_p90)}`);
  }
  if ((point.without_gate ?? 0) > 0) parts.push(`${point.without_gate} with no gate at all`);
  return parts.join(" · ");
}

/** How long something has been waiting, for the in-review list and the open questions. */
export function waitWords(hoursWaiting: number): string {
  if (hoursWaiting < 1) return `${Math.max(Math.round(hoursWaiting * 60), 1)} min`;
  if (hoursWaiting < 48) return `${hoursWaiting.toFixed(1)} h`;
  return `${Math.round(hoursWaiting / 24)} days`;
}
