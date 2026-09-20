/**
 * What the analytics response *means*, separated from how it is drawn.
 *
 * Everything here is a pure function of the payload, for the same reason
 * `analyticsGeometry.ts` is: the honest half of a chart is the arithmetic, and jsdom
 * can check all of it. The rules these functions encode are the ones the page would
 * otherwise get wrong quietly -- when a delta may be shown at all, what window it is
 * attributed to, and where "we do not know" has to be said instead of a number.
 *
 * `docs/analytics-design.md` §9 is the specification for all three states below, and
 * the sentence worth carrying into every function in this file is its last:
 * **never substitute zero for unknown.**
 */

import type {
  AnalyticsCoverage,
  AnalyticsRange,
  AnalyticsResponse,
  BacklogPoint,
  HolderPoint,
  StuckGroup,
} from "../api/types";
// Type-only, and deliberately so: `analyticsSecondSet.ts` imports this file for its
// formatters, so a value import back would be a cycle. TypeScript erases this one.
import type { DeltaBaseline } from "./analyticsSecondSet";

/**
 * Below this many days of history, trends are suppressed and values are not (§9.2).
 *
 * Fourteen days is two weekly cycles, which is the shortest span over which "is the
 * backlog growing" is a question rather than noise. It is a constant here, beside the
 * function that applies it, so the next person changing it can see what it is for:
 * raising it hides more trends, lowering it starts drawing lines through noise.
 */
export const THIN_HISTORY_DAYS = 14;

/** How much history the store is prepared to claim, which decides what may be drawn. */
export type HistoryDepth = "none" | "thin" | "full";

export interface History {
  depth: HistoryDepth;
  /** Days since the coverage baseline, or null when there is no baseline at all. */
  days: number | null;
}

const MONTHS = [
  "Jan",
  "Feb",
  "Mar",
  "Apr",
  "May",
  "Jun",
  "Jul",
  "Aug",
  "Sep",
  "Oct",
  "Nov",
  "Dec",
];

/**
 * A `YYYY-MM-DD` bucket key as a person reads it.
 *
 * Parsed by hand rather than through `new Date(...)`, which reads a bare date as UTC
 * midnight and then formats it in the reader's own zone -- so a bucket the server
 * computed in `America/Chicago` would render as the day before for anyone east of it.
 * The string already *is* the local day the API bucketed in; there is nothing to
 * convert and everything to lose by trying.
 */
export function formatDay(day: string, withYear = false): string {
  const parts = day.split("-");
  const year = parts[0] ?? "";
  const month = Number(parts[1] ?? "0");
  const date = Number(parts[2] ?? "0");
  const name = MONTHS[month - 1];
  if (!name || !Number.isFinite(date)) return day;
  return withYear ? `${date} ${name} ${year}` : `${date} ${name}`;
}

/**
 * An instant as a day, in the zone the API bucketed the whole response in.
 *
 * `Intl` is used to find *which day* it is in that zone and not to spell it: asking it
 * for a month name gives "Sept" where {@link formatDay} gives "Sep", and the two
 * appear within a centimetre of each other on this page. So the zone conversion goes
 * through a fixed `en-CA` yyyy-mm-dd and the spelling goes through one table.
 */
export function formatInstant(iso: string | null | undefined, timeZone: string): string | null {
  if (!iso) return null;
  const instant = new Date(iso);
  if (Number.isNaN(instant.getTime())) return null;
  try {
    return formatDay(
      new Intl.DateTimeFormat("en-CA", {
        timeZone,
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
      }).format(instant),
      true,
    );
  } catch {
    // An unrecognised IANA name is the server's problem, not a reason to render
    // nothing: fall back to UTC and still say a date.
    return `${instant.getUTCDate()} ${MONTHS[instant.getUTCMonth()] ?? ""} ${instant.getUTCFullYear()}`;
  }
}

/** How a bucket is named in a readout, which depends on how wide it is. */
export function formatBucket(day: string, bucket: AnalyticsRange["bucket"]): string {
  if (bucket === "week") return `week of ${formatDay(day)}`;
  if (bucket === "month") return `month of ${formatDay(day)}`;
  return formatDay(day);
}

/**
 * Which of §9's three states the page is in.
 *
 * `baseline_at` being null is the *only* signal for "nothing has happened here": the
 * API returns it for exactly that case and populates the totals anyway, which is what
 * lets the page say "no history yet" rather than draw an empty axis (§7.4).
 */
export function historyOf(coverage: AnalyticsCoverage, now: Date): History {
  const baseline = coverage.baseline_at ? new Date(coverage.baseline_at) : null;
  if (!baseline || Number.isNaN(baseline.getTime())) return { depth: "none", days: null };
  const days = Math.max(0, (now.getTime() - baseline.getTime()) / 86_400_000);
  return { depth: days < THIN_HISTORY_DAYS ? "thin" : "full", days: Math.floor(days) };
}

/** The open-task level, with however many impossible values had to be floored. */
export interface OpenLevel {
  values: number[];
  /** How many buckets the store put below zero. A store this page can still draw. */
  clamped: number;
}

/**
 * The backlog level, floored at zero (§4.3, and §12's page-side defence in depth).
 *
 * A negative open count is impossible, and it is what a store imported before
 * task-371's clamp produces: three tasks whose logs record a close twenty hours before
 * git first saw the file open the series at −3. The floor belongs to the import and
 * this is the second line of it, because a store imported once by a version without
 * that rule is still read by this page. The count of floored buckets is returned
 * rather than swallowed -- it becomes a coverage warning, because silently drawing a
 * plausible line over an impossible one is the failure mode this exists to avoid.
 */
export function openLevel(backlog: readonly BacklogPoint[]): OpenLevel {
  let clamped = 0;
  const values = backlog.map((point) => {
    if (point.open_count < 0) {
      clamped += 1;
      return 0;
    }
    return point.open_count;
  });
  return { values, clamped };
}

/** The sentence a floored backlog puts in the coverage footer, or null when clean. */
export function clampWarning(level: OpenLevel): string | null {
  if (level.clamped === 0) return null;
  const buckets = level.clamped === 1 ? "one bucket" : `${level.clamped} buckets`;
  return (
    `The backlog level was below zero in ${buckets} and is drawn at zero there. ` +
    "That is impossible rather than merely surprising, and it means this store was " +
    "imported before reconstructed creation times were floored at the earliest " +
    "evidence the task existed."
  );
}

/** Whether the tile's number went up, down or nowhere. */
export type Direction = "up" | "down" | "flat";

/** Whether that direction is good news, bad news, or neither. */
export type Tone = "good" | "bad" | "neutral";

export interface SummaryTile {
  key: string;
  label: string;
  count: number;
  /** The `status` filter this count opens on the Tasks surface. */
  status: string;
  /**
   * The change, or null where §9 forbids claiming one. `kind` separates a difference
   * between two levels from a count of things that happened, because "▲ 9" and "+57"
   * are different claims and an arrow on the second one would be a false comparison.
   */
  delta: { value: number; direction: Direction; tone: Tone; kind: "change" | "flow" } | null;
  /** Why there is no delta, in words a reader can act on. Null when there is one. */
  suppressed: string | null;
  /** How the number was arrived at, for the tile's accessible description. */
  basis: string;
}

function directionOf(value: number): Direction {
  if (value > 0) return "up";
  if (value < 0) return "down";
  return "flat";
}

function toneOf(direction: Direction, risingIs: Tone): Tone {
  if (direction === "flat") return "neutral";
  if (risingIs === "neutral") return "neutral";
  if (direction === "up") return risingIs;
  return risingIs === "good" ? "bad" : "good";
}

function sum(values: readonly number[]): number {
  return values.reduce((total, value) => total + value, 0);
}

/**
 * The window a delta is attributed to, in words (§9.3), **superseded by §19.1**.
 *
 * Kept because it is still the right answer for a page-level statement about the whole
 * response, and it is what the coverage footer says. The summary tiles no longer use
 * it: their baseline is `max(range.start, native_from)`, which is a different date and
 * a different sentence, and `deltaBaseline` in `analyticsSecondSet.ts` is where that
 * lives. Two functions rather than one flag, because the two answers differ whenever
 * native history is younger than the window -- which is every range this project
 * offers, for months yet.
 */
export function deltaWindow(range: AnalyticsRange, coverage: AnalyticsCoverage): string {
  if (coverage.complete) {
    if (range.key === "30d") return "in 30 days";
    if (range.key === "90d") return "in 90 days";
    if (range.key === "12m") return "in 12 months";
    return "over all history";
  }
  const start = formatInstant(range.start, range.timezone);
  return start ? `since ${start}` : "over the window shown";
}

/**
 * The five counts, each with the change §8.2 says is the entire reason for the page.
 *
 * Every delta below is computed from a series in the same payload rather than guessed,
 * and each tile carries the arithmetic in `basis` so a reader who doubts a number has
 * somewhere to go. The two flows -- created and completed -- are counts of events in
 * the window rather than differences of two levels, which is why they are `kind:
 * "flow"`: a reopened task closes twice and no difference of totals would say so.
 *
 * **The window is the baseline's, not the range's** (§19.1). A `baseline` whose day is
 * later than `range.start` is native history beginning inside the window, and every
 * sum below is taken from that day forward. The previous version summed the whole
 * range against the backfilled floor and produced *"149 open ▲ 145 more"* -- a number
 * four short of its own total, which is what a delta against a reconstructed instant
 * amounts to. The buckets before the baseline are still drawn on the charts, where
 * they are hatched and captioned; they are just not differenced.
 */
export function summaryTiles(
  data: AnalyticsResponse,
  history: History,
  baseline: DeltaBaseline,
): SummaryTile[] {
  const from = baseline.day;
  const inWindow = <T,>(points: readonly T[], key: (point: T) => string): T[] =>
    from === null ? [] : points.filter((point) => key(point) >= from);

  const backlog = inWindow(data.backlog ?? [], (point) => point.day);
  const holders = inWindow(data.holders ?? [], (point) => point.day);
  const throughput = inWindow(data.throughput ?? [], (point) => point.bucket);
  const opened = sum(backlog.map((point) => point.opened));
  const closed = sum(backlog.map((point) => point.closed));
  const completedInWindow = sum(throughput.map((point) => point.tasks_completed));
  const first = holders[0];
  const last = holders[holders.length - 1];
  const window = baseline.words;

  const suppressed =
    history.depth === "none"
      ? "no history yet"
      : history.depth === "thin"
        ? `only ${history.days} days of history`
        : baseline.suppressed;

  const tile = (
    key: string,
    label: string,
    count: number,
    status: string,
    value: number | null,
    risingIs: Tone,
    kind: "change" | "flow",
    basis: string,
  ): SummaryTile => {
    if (suppressed !== null || value === null) {
      return { key, label, count, status, delta: null, suppressed: suppressed ?? "not measured", basis };
    }
    const direction = directionOf(value);
    return {
      key,
      label,
      count,
      status,
      delta: { value, direction, tone: toneOf(direction, risingIs), kind },
      suppressed: null,
      basis,
    };
  };

  const holderDelta = (band: "human" | "external"): number | null =>
    first && last ? last[band] - first[band] : null;

  return [
    tile(
      "open",
      "Open",
      data.totals.open,
      "open",
      backlog.length > 0 ? opened - closed : null,
      "bad",
      "change",
      `${opened} opened less ${closed} closed ${window}`,
    ),
    tile(
      "created",
      "Created",
      data.totals.total,
      "all",
      backlog.length > 0 ? opened : null,
      "neutral",
      "flow",
      `tasks created ${window}`,
    ),
    tile(
      "completed",
      "Completed",
      data.totals.completed,
      "closed",
      throughput.length > 0 ? completedInWindow : null,
      "good",
      "flow",
      `distinct tasks completed ${window}`,
    ),
    tile(
      "human",
      "Waiting on you",
      data.totals.waiting_for_human + data.totals.awaiting_input,
      "human",
      holderDelta("human"),
      "bad",
      "change",
      `tasks held by a human, first bucket ${window} to last`,
    ),
    tile(
      "external",
      "Blocked",
      data.totals.blocked,
      "external",
      holderDelta("external"),
      "bad",
      "change",
      `tasks held by something outside the project, first bucket ${window} to last`,
    ),
  ];
}

/** A number with one decimal place, or an em dash where there is no number (§9.3). */
export function days(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return `${value.toFixed(1)}d`;
}

/** The always-present line above the backlog chart, which is how a phone reads it. */
export function backlogReadout(point: BacklogPoint, bucket: AnalyticsRange["bucket"]): string {
  const level = Math.max(0, point.open_count);
  return `${formatBucket(point.day, bucket)} · ${level} open · ${point.opened} opened · ${point.closed} closed`;
}

/** Below this many completions in a bucket, a percentile is not a number (§8.4). */
export const PERCENTILE_MIN_SAMPLE = 3;

/** The readout for the holder stack. */
export function holderReadout(point: HolderPoint, bucket: AnalyticsRange["bucket"]): string {
  return `${formatBucket(point.day, bucket)} · ${point.agent} agent · ${point.human} human · ${point.external} external`;
}

/** The app's own word for who is holding a task, so a row reads as a sentence. */
export function holderWords(ball: string): string {
  if (ball === "human") return "waiting on you";
  if (ball === "agent") return "with an agent";
  if (ball === "external") return "blocked outside the project";
  return ball;
}

/** One stuck row, as §8.6 words it. */
export function stuckPhrase(group: StuckGroup): string {
  const plural = group.tasks === 1 ? "task" : "tasks";
  return (
    `${group.tasks} ${plural} ${holderWords(group.ball)} — ${group.ball_reason} · ` +
    `longest ${Math.round(group.max_days_held)} days`
  );
}

/** An age in the shortest form that is still exact enough to act on. */
export function ageWords(ageDays: number): string {
  if (ageDays < 1) return "today";
  const whole = Math.round(ageDays);
  return whole === 1 ? "1 day" : `${whole} days`;
}
