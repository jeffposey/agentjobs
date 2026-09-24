import { useId } from "react";

import type {
  AnalyticsRange,
  CostPerTaskPoint,
  FinishPoint,
  GatePoint,
  MachinePoint,
  RunPoint,
  ReviewPoint,
  SegmentPoint,
} from "../api/types";
import {
  HitTargets,
  Legend,
  LegendKey,
  Patterns,
  Readout,
  SVG_CLASS,
  SelectionRule,
  SeriesName,
  SeriesTable,
  BlankBuckets,
  XTicks,
  YAxis,
  keyboardSelect,
  type Selectable,
} from "./AnalyticsCharts";
import {
  COUNT_PADDING,
  SMALL_VIEWBOX,
  VALUE_PADDING,
  VIEWBOX,
  axisMax,
  columnBars,
  groupedStacks,
  linePath,
  plotBox,
  stackedBars,
  type BarRect,
} from "./analyticsGeometry";
import { PERCENTILE_MIN_SAMPLE, formatBucket, formatDay } from "./analyticsSeries";
import {
  SEGMENTS,
  blankUnderSample,
  costReadout,
  counted,
  finishReadout,
  gateReadout,
  hours,
  minutes,
  reviewReadout,
  runReadout,
  segmentBlanks,
  segmentReadout,
  segmentStack,
  segmentValue,
  unknownBuckets,
} from "./analyticsSecondSet";

/**
 * The second set's panels: where the time goes, finishes and gates, runs, review and
 * cost per completed task (`docs/analytics-design.md` §18, placed by §19.4).
 *
 * A separate file from `AnalyticsCharts.tsx` because that one is task-373's four
 * shapes and their conventions, and this is seven more panels drawn with the same
 * shapes. Everything shared is imported from there rather than copied -- the hit
 * targets, the selection rule, the axes, the hidden series table, the legend and the
 * readout are all one implementation, so a change to how a chart is read reaches every
 * chart on the page.
 *
 * **Three conventions, unchanged and worth restating because every panel below obeys
 * them.** Nothing is hover-only: a value is printed, or in the readout, or in the
 * hidden table. Colour is never the only channel: every series carries a fill pattern
 * and a name as well. And nothing measures itself: the geometry is pure functions over
 * a fixed viewBox, so jsdom can check the arithmetic and the browser does the scaling.
 *
 * **One convention is new.** §19.5 replaced the p50-to-p90 band with *p90 on tap*: the
 * chart draws the median, and the readout -- which always describes the selected
 * bucket -- carries both percentiles. A band was the blob the owner could not read,
 * and §10.4 had already made the tap the primary gesture, so the information moved to
 * where the gesture already went.
 */

/** The value-axis plot every duration chart here shares: room for `300 h` on the left. */
const VALUE_PLOT = plotBox(VALUE_PADDING);

/** The counted-axis plot, with the same room above the plot for a series name. */
const COUNT_PLOT = plotBox(COUNT_PADDING);

/** The small square plot each of the three cost charts is drawn in. */
const SMALL_PLOT = plotBox(COUNT_PADDING, SMALL_VIEWBOX);

/** How many pixels of the readout a phone can be asked to read before it wraps badly. */
function svgProps(testId: string, label: string) {
  return {
    viewBox: `0 0 ${VIEWBOX.width} ${VIEWBOX.height}`,
    className: SVG_CLASS,
    role: "img" as const,
    tabIndex: 0,
    "data-testid": testId,
    "aria-label": label,
  };
}

// ---------------------------------------------------------------------------
// S1 -- where the time goes
// ---------------------------------------------------------------------------

/** One fill per lifecycle segment, in stacking order. Queue first: it is the big one. */
const SEGMENT_FILL: Record<string, string> = {
  queue: "fill-amber-300",
  work: "fill-emerald-400",
  waiting: "fill-violet-300",
  review: "fill-sky-400",
  finish: "fill-rose-400",
};

/**
 * The five patterns that make the stack readable without colour (§10.3).
 *
 * Five is more than the two `Patterns` defines, and they are defined here rather than
 * there because they belong to one chart: a legend of five entries told apart only by
 * hue is exactly what §10.3 exists to prevent, and a reader with a monochrome screen
 * or deuteranopia has to be able to count the bands.
 */
function SegmentPatterns({ prefix }: { prefix: string }) {
  return (
    <defs>
      <pattern id={`${prefix}-work`} width={5} height={5} patternUnits="userSpaceOnUse">
        <circle cx={1.5} cy={1.5} r={1} className="fill-dark-bg" opacity={0.7} />
      </pattern>
      <pattern
        id={`${prefix}-waiting`}
        width={5}
        height={5}
        patternTransform="rotate(45)"
        patternUnits="userSpaceOnUse"
      >
        <line x1={0} y1={0} x2={0} y2={5} className="stroke-dark-bg" strokeWidth={1.4} />
      </pattern>
      <pattern
        id={`${prefix}-review`}
        width={5}
        height={5}
        patternTransform="rotate(-45)"
        patternUnits="userSpaceOnUse"
      >
        <line x1={0} y1={0} x2={0} y2={5} className="stroke-dark-bg" strokeWidth={1.4} />
      </pattern>
      <pattern id={`${prefix}-finish`} width={4} height={4} patternUnits="userSpaceOnUse">
        <path d="M0 0 H4 M0 0 V4" className="stroke-dark-bg" strokeWidth={1} fill="none" />
      </pattern>
    </defs>
  );
}

/**
 * S1: where a completed task's time went, as a stack of five medians per week.
 *
 * *Stacked bars, not a stacked area.* Weekly buckets are discrete, and an area implies
 * a level between them that nobody measured (§18.1). The height is the sum of five
 * medians and therefore **not** the median total -- S2 supplies that, in the readout,
 * and the caption says so once rather than leaving a reader to discover it.
 *
 * A bucket under `PERCENTILE_MIN_SAMPLE` is left visibly blank and outlined, never
 * interpolated and never drawn as five noughts: a week in which two tasks closed has
 * no median, and a bar of height zero would say the work took no time.
 */
export function SegmentsChart({
  points,
  bucket,
  selected,
  onSelect,
}: {
  points: readonly SegmentPoint[];
  bucket: AnalyticsRange["bucket"];
} & Selectable) {
  const ids = useId();
  const stack = segmentStack(points);
  const blanks = segmentBlanks(points);
  const max = axisMax(points.map((point, index) => stack.reduce((sum, one) => sum + (one[index] ?? 0), 0)));
  const bars = stackedBars(stack, max, VALUE_PLOT);
  const count = points.length;
  const current = points[Math.min(selected, Math.max(count - 1, 0))];

  return (
    <figure className="mt-1">
      <Readout testId="segments-readout">{segmentReadout(current, bucket)}</Readout>
      <svg
        {...svgProps(
          "segments-chart",
          `Median hours per lifecycle segment for the tasks completed in each ${bucket}.`,
        )}
        onKeyDown={(event) => keyboardSelect(event, selected, count, onSelect)}
      >
        <SegmentPatterns prefix={ids} />
        <YAxis box={VALUE_PLOT} max={max} format={(value) => hours(value)} />
        <BlankBuckets
          box={VALUE_PLOT}
          unknown={blanks}
          reason={`Fewer than ${PERCENTILE_MIN_SAMPLE} tasks completed: no median.`}
        />
        {SEGMENTS.map((segment, index) => (
          <g key={segment}>
            {(bars[index] ?? []).map((bar: BarRect) => (
              <g key={`${segment}${bar.index}`}>
                <rect
                  {...bar}
                  className={SEGMENT_FILL[segment] ?? "fill-sky-400"}
                  opacity={0.85}
                  data-testid={`segment-${segment}`}
                />
                {segment !== "queue" && <rect {...bar} fill={`url(#${ids}-${segment})`} />}
              </g>
            ))}
          </g>
        ))}
        <SeriesName box={VALUE_PLOT} label="median hours per task" className="fill-dark-muted" />
        <SelectionRule box={VALUE_PLOT} count={count} selected={selected} />
        <HitTargets
          box={VALUE_PLOT}
          count={count}
          selected={selected}
          onSelect={onSelect}
          labelFor={(index) => segmentReadout(points[index], bucket)}
        />
        <XTicks box={VALUE_PLOT} labels={points.map((point) => formatDay(point.bucket))} />
      </svg>
      <Legend>
        {SEGMENTS.map((segment) => (
          <LegendKey
            key={segment}
            label={segment}
            swatch={
              <>
                <rect width={12} height={12} className={SEGMENT_FILL[segment] ?? "fill-sky-400"} />
                {segment === "work" && <circle cx={6} cy={6} r={2} className="fill-dark-bg" />}
                {segment === "waiting" && (
                  <line x1={0} y1={12} x2={12} y2={0} className="stroke-dark-bg" strokeWidth={2} />
                )}
                {segment === "review" && (
                  <line x1={0} y1={0} x2={12} y2={12} className="stroke-dark-bg" strokeWidth={2} />
                )}
                {segment === "finish" && (
                  <path d="M0 6 H12 M6 0 V12" className="stroke-dark-bg" strokeWidth={1.5} />
                )}
              </>
            }
          />
        ))}
      </Legend>
      <p className="mt-1 text-xs text-dark-muted">
        The stack is five medians added together, which is not the median total — the
        readout carries that separately.
      </p>
      <SeriesTable
        testId="segments-series"
        caption="Median hours per segment, and the total, for the tasks completed in each bucket"
        columns={["Bucket", ...SEGMENTS.map((segment) => segment), "Total p50", "Total p90", "Tasks"]}
        rows={points.map((point) => [
          formatBucket(point.bucket, bucket),
          ...SEGMENTS.map((segment) =>
            point.sample < PERCENTILE_MIN_SAMPLE
              ? "not measured"
              : hours(segmentValue(point, segment, 50)),
          ),
          point.sample < PERCENTILE_MIN_SAMPLE ? "not measured" : hours(point.total_p50_hours),
          point.sample < PERCENTILE_MIN_SAMPLE ? "not measured" : hours(point.total_p90_hours),
          String(point.sample),
        ])}
      />
    </figure>
  );
}

// ---------------------------------------------------------------------------
// F1 -- finishes per week by outcome
// ---------------------------------------------------------------------------

const FINISH_OUTCOMES = ["finished", "escalated", "declined", "interrupted"] as const;
const FINISH_FILL: Record<string, string> = {
  finished: "fill-emerald-400",
  escalated: "fill-amber-300",
  declined: "fill-violet-300",
  interrupted: "fill-rose-400",
};

/**
 * F1: what the scripted finish did each week, `finished` at the bottom of the stack.
 *
 * The bottom of a stack is the band the eye measures against the axis, so it holds the
 * outcome the project wants. G3's green rate and F4's runway wait are in this panel's
 * readout rather than on charts of their own: a rate over single-digit counts drawn as
 * a line is noise, and a value that is zero nine weeks in ten is decoration (§18.4).
 */
export function FinishChart({
  points,
  bucket,
  selected,
  onSelect,
}: {
  points: readonly FinishPoint[];
  bucket: AnalyticsRange["bucket"];
} & Selectable) {
  const ids = useId();
  const hatch = `${ids}-hatch`;
  const dots = `${ids}-dots`;
  const series = FINISH_OUTCOMES.map((outcome) => points.map((point) => point[outcome] ?? 0));
  const max = axisMax(points.map((_, index) => series.reduce((sum, one) => sum + (one[index] ?? 0), 0)));
  const bars = stackedBars(series, max, COUNT_PLOT);
  const count = points.length;
  const current = points[Math.min(selected, Math.max(count - 1, 0))];
  const total = series.reduce((sum, one) => sum + one.reduce((inner, value) => inner + value, 0), 0);

  return (
    <figure className="mt-1">
      <Readout testId="finishes-readout">{finishReadout(current, bucket)}</Readout>
      <svg
        {...svgProps("finishes-chart", `${total} finishes in this window, split by outcome.`)}
        onKeyDown={(event) => keyboardSelect(event, selected, count, onSelect)}
      >
        <Patterns hatch={hatch} dots={dots} />
        <YAxis box={COUNT_PLOT} max={max} />
        {FINISH_OUTCOMES.map((outcome, index) => (
          <g key={outcome}>
            {(bars[index] ?? []).map((bar: BarRect) => (
              <g key={`${outcome}${bar.index}`}>
                <rect
                  {...bar}
                  className={FINISH_FILL[outcome] ?? "fill-sky-400"}
                  opacity={0.85}
                  data-testid={`finish-${outcome}`}
                />
                {outcome === "escalated" && <rect {...bar} fill={`url(#${hatch})`} />}
                {outcome === "interrupted" && <rect {...bar} fill={`url(#${dots})`} />}
              </g>
            ))}
          </g>
        ))}
        <SeriesName box={COUNT_PLOT} label="finishes" className="fill-dark-muted" />
        <SelectionRule box={COUNT_PLOT} count={count} selected={selected} />
        <HitTargets
          box={COUNT_PLOT}
          count={count}
          selected={selected}
          onSelect={onSelect}
          labelFor={(index) => finishReadout(points[index], bucket)}
        />
        <XTicks box={COUNT_PLOT} labels={points.map((point) => formatDay(point.bucket))} />
      </svg>
      <Legend>
        {FINISH_OUTCOMES.map((outcome) => (
          <LegendKey
            key={outcome}
            label={outcome}
            swatch={
              <>
                <rect width={12} height={12} className={FINISH_FILL[outcome] ?? "fill-sky-400"} />
                {outcome === "escalated" && (
                  <line x1={0} y1={12} x2={12} y2={0} className="stroke-dark-bg" strokeWidth={2} />
                )}
                {outcome === "interrupted" && (
                  <circle cx={6} cy={6} r={2} className="fill-dark-bg" />
                )}
              </>
            }
          />
        ))}
      </Legend>
      <SeriesTable
        testId="finishes-series"
        caption="Finishes per bucket by outcome, with the finished duration"
        columns={["Bucket", ...FINISH_OUTCOMES.map((outcome) => outcome), "p50", "p90"]}
        rows={points.map((point) => [
          formatBucket(point.bucket, bucket),
          ...FINISH_OUTCOMES.map((outcome) => String(point[outcome] ?? 0)),
          (point.sample ?? 0) > 0 ? minutes(point.duration_p50_min) : "not measured",
          (point.sample ?? 0) > 0 ? minutes(point.duration_p90_min) : "not measured",
        ])}
      />
    </figure>
  );
}

// ---------------------------------------------------------------------------
// F2 and G1 -- the two durations that share an axis
// ---------------------------------------------------------------------------

/**
 * F2 and G1 on one chart, sharing one axis, because both are minutes and the gate is
 * most of a finish (§18.4).
 *
 * This is **not** §8.4's rejected two-axis chart: there is one axis, and the two lines
 * are directly comparable on it -- the distance between them is the part of a finish
 * that is not the gate, which is the thing worth seeing. Each line breaks wherever its
 * bucket is under `PERCENTILE_MIN_SAMPLE`, because a percentile over two gates is not
 * a number and a line drawn through it would claim a trend nobody measured.
 */
export function DurationChart({
  finishes,
  gates,
  bucket,
  selected,
  onSelect,
}: {
  finishes: readonly FinishPoint[];
  gates: readonly GatePoint[];
  bucket: AnalyticsRange["bucket"];
} & Selectable) {
  const ids = useId();
  const hatch = `${ids}-hatch`;
  const dots = `${ids}-dots`;
  // The two series are bucketed the same way by the API (§21.1's `bucket` field), so
  // the finish spine is the chart's spine and the gate series is aligned onto it by
  // bucket key rather than by position -- a series that starts later has fewer points.
  const byBucket = new Map(gates.map((point) => [point.bucket, point]));
  const spine = finishes.length >= gates.length ? finishes.map((point) => point.bucket) : gates.map((point) => point.bucket);
  const finishByBucket = new Map(finishes.map((point) => [point.bucket, point]));
  const finishLine = blankUnderSample(
    spine,
    (key) => finishByBucket.get(key)?.duration_p50_min ?? null,
    (key) => finishByBucket.get(key)?.sample ?? 0,
  );
  const gateLine = blankUnderSample(
    spine,
    (key) => byBucket.get(key)?.duration_p50_min ?? null,
    (key) => byBucket.get(key)?.sample ?? 0,
  );
  const max = axisMax([...finishLine, ...gateLine]);
  const count = spine.length;
  const index = Math.min(selected, Math.max(count - 1, 0));
  const key = spine[index];
  const blanks = unknownBuckets(finishLine).map((blank, at) => blank && (unknownBuckets(gateLine)[at] ?? false));

  return (
    <figure className="mt-1">
      <Readout testId="durations-readout">
        <span className="block">{finishReadout(key ? finishByBucket.get(key) : undefined, bucket)}</span>
        <span className="mt-1 block">{gateReadout(key ? byBucket.get(key) : undefined, bucket)}</span>
      </Readout>
      <svg
        {...svgProps(
          "durations-chart",
          "Median minutes per finish and per full green gate, on one axis.",
        )}
        onKeyDown={(event) => keyboardSelect(event, selected, count, onSelect)}
      >
        <Patterns hatch={hatch} dots={dots} />
        <YAxis box={VALUE_PLOT} max={max} format={(value) => minutes(value)} />
        <BlankBuckets
          box={VALUE_PLOT}
          unknown={blanks}
          reason={`Fewer than ${PERCENTILE_MIN_SAMPLE} in this bucket: no median.`}
        />
        <path
          d={linePath(finishLine, max, VALUE_PLOT)}
          className="stroke-emerald-400"
          fill="none"
          strokeWidth={2}
          data-testid="finish-duration-line"
        />
        <path
          d={linePath(gateLine, max, VALUE_PLOT)}
          className="stroke-sky-300"
          fill="none"
          strokeWidth={2}
          strokeDasharray="5 3"
          data-testid="gate-duration-line"
        />
        <SeriesName box={VALUE_PLOT} label="finish (solid) · gate (dashed)" className="fill-dark-muted" />
        <SelectionRule box={VALUE_PLOT} count={count} selected={selected} />
        <HitTargets
          box={VALUE_PLOT}
          count={count}
          selected={selected}
          onSelect={onSelect}
          labelFor={(at) => {
            const bucketKey = spine[at];
            return bucketKey
              ? `${finishReadout(finishByBucket.get(bucketKey), bucket)} · ${gateReadout(byBucket.get(bucketKey), bucket)}`
              : "";
          }}
        />
        <XTicks box={VALUE_PLOT} labels={spine.map((day) => formatDay(day))} />
      </svg>
      <Legend>
        <LegendKey
          label="a finish, end to end"
          swatch={<rect y={5} width={12} height={2} className="fill-emerald-400" />}
        />
        <LegendKey
          label="the full gate inside it, dashed"
          swatch={
            <>
              <rect y={5} width={5} height={2} className="fill-sky-300" />
              <rect x={8} y={5} width={4} height={2} className="fill-sky-300" />
            </>
          }
        />
      </Legend>
      <SeriesTable
        testId="durations-series"
        caption="Median minutes per finish and per full green gate, per bucket"
        columns={["Bucket", "Finish p50", "Finish p90", "Gate p50", "Gate p90", "Green"]}
        rows={spine.map((day) => {
          const finish = finishByBucket.get(day);
          const gate = byBucket.get(day);
          return [
            formatBucket(day, bucket),
            (finish?.sample ?? 0) > 0 ? minutes(finish?.duration_p50_min) : "not measured",
            (finish?.sample ?? 0) > 0 ? minutes(finish?.duration_p90_min) : "not measured",
            (gate?.sample ?? 0) > 0 ? minutes(gate?.duration_p50_min) : "not measured",
            (gate?.sample ?? 0) > 0 ? minutes(gate?.duration_p90_min) : "not measured",
            (gate?.full ?? 0) > 0 ? `${gate?.passed ?? 0} of ${gate?.full}` : "—",
          ];
        })}
      />
    </figure>
  );
}

// ---------------------------------------------------------------------------
// R-1, R-2, R-6 -- runs, the hours they took, and the hours a quota stopped
// ---------------------------------------------------------------------------

/**
 * R-1 and R-2 side by side in each bucket, on two axes, with R-6 stacked on the hours.
 *
 * **The one case §8.4's argument against two axes does not cover.** There the two
 * series were an overlay -- a line drawn over bars it had nothing to do with -- and a
 * reader had to work out which axis each belonged to. Here they are two bars standing
 * next to each other in the same bucket, each beside its own axis, answering one
 * question: how much machine went into this day. Neither is drawn on top of the other,
 * so there is nothing to misattribute.
 *
 * The paused hours sit above the agent-hours bar rather than beside it, because they
 * are machine hours too -- hours the quota took, which produced nothing (§18.5's R-6).
 */
export function RunsChart({
  points,
  machine,
  bucket,
  selected,
  onSelect,
}: {
  points: readonly RunPoint[];
  machine: readonly MachinePoint[];
  bucket: AnalyticsRange["bucket"];
} & Selectable) {
  const ids = useId();
  const hatch = `${ids}-hatch`;
  const dots = `${ids}-dots`;
  const byBucket = new Map(machine.map((point) => [point.bucket, point]));
  const runs = points.map((point) => point.runs ?? 0);
  const agentHours = points.map((point) => point.agent_hours ?? 0);
  const pausedHours = points.map((point) => byBucket.get(point.bucket)?.paused_run_hours ?? 0);
  const runMax = axisMax(runs);
  const hourMax = axisMax(agentHours.map((value, index) => value + (pausedHours[index] ?? 0)));
  const [runGroup, hourGroup] = groupedStacks(
    [[runs], [agentHours, pausedHours]],
    [runMax, hourMax],
    COUNT_PLOT,
  );
  const count = points.length;
  const current = points[Math.min(selected, Math.max(count - 1, 0))];
  const paused = pausedHours.reduce((sum, value) => sum + value, 0);

  return (
    <figure className="mt-1">
      <Readout testId="runs-readout">
        {runReadout(current, current ? byBucket.get(current.bucket) : undefined, bucket)}
      </Readout>
      <svg
        {...svgProps(
          "runs-chart",
          `Dispatched runs and the agent-hours they took, per ${bucket}, on two axes.`,
        )}
        onKeyDown={(event) => keyboardSelect(event, selected, count, onSelect)}
      >
        <Patterns hatch={hatch} dots={dots} />
        <YAxis box={COUNT_PLOT} max={runMax} />
        {(runGroup?.[0] ?? []).map((bar: BarRect) => (
          <rect key={`n${bar.index}`} {...bar} className="fill-sky-400" data-testid="run-bar" />
        ))}
        {(hourGroup?.[0] ?? []).map((bar: BarRect) => (
          <g key={`h${bar.index}`}>
            <rect {...bar} className="fill-violet-300" opacity={0.8} data-testid="agent-hours-bar" />
            <rect {...bar} fill={`url(#${dots})`} />
          </g>
        ))}
        {(hourGroup?.[1] ?? []).map((bar: BarRect) =>
          bar.height > 0 ? (
            <g key={`p${bar.index}`}>
              <rect {...bar} className="fill-dark-muted" opacity={0.6} data-testid="paused-hours-bar" />
              <rect {...bar} fill={`url(#${hatch})`} />
            </g>
          ) : null,
        )}
        <SeriesName box={COUNT_PLOT} label="runs" className="fill-sky-400" />
        <SeriesName
          box={COUNT_PLOT}
          label={`agent-hours, 0–${hourMax}`}
          className="fill-violet-300"
          anchor="end"
        />
        <SelectionRule box={COUNT_PLOT} count={count} selected={selected} />
        <HitTargets
          box={COUNT_PLOT}
          count={count}
          selected={selected}
          onSelect={onSelect}
          labelFor={(index) => {
            const point = points[index];
            return point ? runReadout(point, byBucket.get(point.bucket), bucket) : "";
          }}
        />
        <XTicks box={COUNT_PLOT} labels={points.map((point) => formatDay(point.bucket))} />
      </svg>
      <Legend>
        <LegendKey
          label="runs, left axis"
          swatch={<rect width={12} height={12} className="fill-sky-400" />}
        />
        <LegendKey
          label="agent-hours, right axis"
          swatch={
            <>
              <rect width={12} height={12} className="fill-violet-300" opacity={0.7} />
              <circle cx={4} cy={4} r={1.5} className="fill-dark-text" />
              <circle cx={9} cy={9} r={1.5} className="fill-dark-text" />
            </>
          }
        />
        {paused > 0 && (
          <LegendKey
            label={`paused on a usage limit, above the hours: ${paused.toFixed(1)} run-hours`}
            swatch={
              <>
                <rect width={12} height={12} className="fill-dark-muted" opacity={0.6} />
                <line x1={0} y1={12} x2={12} y2={0} className="stroke-dark-text" strokeWidth={2} />
              </>
            }
          />
        )}
      </Legend>
      <SeriesTable
        testId="runs-series"
        caption="Runs, agent-hours and paused run-hours per bucket"
        columns={["Bucket", "Runs", "Agent-hours", "Paused run-hours", "Run p50", "Still running"]}
        rows={points.map((point, index) => [
          formatBucket(point.bucket, bucket),
          String(point.runs ?? 0),
          (point.agent_hours ?? 0).toFixed(1),
          (pausedHours[index] ?? 0).toFixed(1),
          (point.sample ?? 0) > 0 ? minutes(point.duration_p50_min) : "not measured",
          String(point.in_flight ?? 0),
        ])}
      />
    </figure>
  );
}

// ---------------------------------------------------------------------------
// R-3 -- the outcome mix
// ---------------------------------------------------------------------------

/** Every outcome `task_run` records, `completed` first so it sits at the bottom. */
const RUN_OUTCOMES = ["completed", "interrupted", "cancelled", "finished_without_handoff"];
const RUN_OUTCOME_FILL: Record<string, string> = {
  completed: "fill-emerald-400",
  interrupted: "fill-amber-300",
  cancelled: "fill-dark-muted",
  finished_without_handoff: "fill-violet-300",
};

/**
 * R-3: how the week's runs ended, `completed` at the bottom.
 *
 * The vocabulary is the store's own strings and is not a generated type (§21.2), so
 * this draws the four it knows and folds anything else into *other* rather than
 * dropping it: a new outcome must not silently disappear from a chart of outcomes.
 */
export function RunOutcomeChart({
  points,
  bucket,
  selected,
  onSelect,
}: {
  points: readonly RunPoint[];
  bucket: AnalyticsRange["bucket"];
} & Selectable) {
  const ids = useId();
  const hatch = `${ids}-hatch`;
  const dots = `${ids}-dots`;
  const extras = new Set<string>();
  for (const point of points) {
    for (const key of Object.keys(point.outcomes ?? {})) {
      if (!RUN_OUTCOMES.includes(key)) extras.add(key);
    }
  }
  const keys = [...RUN_OUTCOMES, ...[...extras].sort()];
  const series = keys.map((key) => points.map((point) => point.outcomes?.[key] ?? 0));
  const max = axisMax(points.map((_, index) => series.reduce((sum, one) => sum + (one[index] ?? 0), 0)));
  const bars = stackedBars(series, max, COUNT_PLOT);
  const count = points.length;
  const current = points[Math.min(selected, Math.max(count - 1, 0))];

  return (
    <figure className="mt-1">
      <Readout testId="run-outcomes-readout">{runReadout(current, undefined, bucket)}</Readout>
      <svg
        {...svgProps("run-outcomes-chart", `How the runs in each ${bucket} ended.`)}
        onKeyDown={(event) => keyboardSelect(event, selected, count, onSelect)}
      >
        <Patterns hatch={hatch} dots={dots} />
        <YAxis box={COUNT_PLOT} max={max} />
        {keys.map((key, index) => (
          <g key={key}>
            {(bars[index] ?? []).map((bar: BarRect) => (
              <g key={`${key}${bar.index}`}>
                <rect
                  {...bar}
                  className={RUN_OUTCOME_FILL[key] ?? "fill-sky-400"}
                  opacity={0.85}
                  data-testid={`run-outcome-${key}`}
                />
                {index % 3 === 1 && <rect {...bar} fill={`url(#${hatch})`} />}
                {index % 3 === 2 && <rect {...bar} fill={`url(#${dots})`} />}
              </g>
            ))}
          </g>
        ))}
        <SeriesName box={COUNT_PLOT} label="runs by outcome" className="fill-dark-muted" />
        <SelectionRule box={COUNT_PLOT} count={count} selected={selected} />
        <HitTargets
          box={COUNT_PLOT}
          count={count}
          selected={selected}
          onSelect={onSelect}
          labelFor={(index) => runReadout(points[index], undefined, bucket)}
        />
        <XTicks box={COUNT_PLOT} labels={points.map((point) => formatDay(point.bucket))} />
      </svg>
      <Legend>
        {keys.map((key, index) => (
          <LegendKey
            key={key}
            label={key.replace(/_/g, " ")}
            swatch={
              <>
                <rect width={12} height={12} className={RUN_OUTCOME_FILL[key] ?? "fill-sky-400"} />
                {index % 3 === 1 && (
                  <line x1={0} y1={12} x2={12} y2={0} className="stroke-dark-bg" strokeWidth={2} />
                )}
                {index % 3 === 2 && <circle cx={6} cy={6} r={2} className="fill-dark-bg" />}
              </>
            }
          />
        ))}
      </Legend>
      <SeriesTable
        testId="run-outcomes-series"
        caption="Runs by outcome per bucket"
        columns={["Bucket", ...keys.map((key) => key.replace(/_/g, " ")), "Still running"]}
        rows={points.map((point) => [
          formatBucket(point.bucket, bucket),
          ...keys.map((key) => String(point.outcomes?.[key] ?? 0)),
          String(point.in_flight ?? 0),
        ])}
      />
    </figure>
  );
}

// ---------------------------------------------------------------------------
// R2 and Q-2 -- how long a person took
// ---------------------------------------------------------------------------

/**
 * R2 and Q-2 on one chart: how long a review waited, and how long a question waited.
 *
 * They share a panel because they are the same fact from two directions -- *how long
 * did a person take* -- and they share an axis because both are hours (§18.7). Each
 * line breaks where its bucket is under the minimum sample; R3's first-time approval
 * rate is in the readout, because a fraction of a single-digit count drawn as a line
 * would be noise with a trend painted on it.
 */
export function ReviewChart({
  points,
  bucket,
  selected,
  onSelect,
}: {
  points: readonly ReviewPoint[];
  bucket: AnalyticsRange["bucket"];
} & Selectable) {
  const ids = useId();
  const hatch = `${ids}-hatch`;
  const dots = `${ids}-dots`;
  const waits = blankUnderSample(
    points,
    (point) => point.wait_p50_hours,
    (point) => point.exits ?? 0,
  );
  const answers = blankUnderSample(
    points,
    (point) => point.answer_p50_hours,
    (point) => point.answered ?? 0,
  );
  const max = axisMax([...waits, ...answers]);
  const count = points.length;
  const current = points[Math.min(selected, Math.max(count - 1, 0))];
  const blanks = waits.map((value, index) => value === null && answers[index] === null);

  return (
    <figure className="mt-1">
      <Readout testId="review-readout">{reviewReadout(current, bucket)}</Readout>
      <svg
        {...svgProps(
          "review-chart",
          "Median hours a review waited and a question waited, per bucket.",
        )}
        onKeyDown={(event) => keyboardSelect(event, selected, count, onSelect)}
      >
        <Patterns hatch={hatch} dots={dots} />
        <YAxis box={VALUE_PLOT} max={max} format={(value) => hours(value)} />
        <BlankBuckets
          box={VALUE_PLOT}
          unknown={blanks}
          reason={`Fewer than ${PERCENTILE_MIN_SAMPLE} in this bucket: no median.`}
        />
        <path
          d={linePath(waits, max, VALUE_PLOT)}
          className="stroke-amber-300"
          fill="none"
          strokeWidth={2}
          data-testid="review-wait-line"
        />
        <path
          d={linePath(answers, max, VALUE_PLOT)}
          className="stroke-sky-300"
          fill="none"
          strokeWidth={2}
          strokeDasharray="5 3"
          data-testid="answer-wait-line"
        />
        <SeriesName
          box={VALUE_PLOT}
          label="review wait (solid) · answer wait (dashed)"
          className="fill-dark-muted"
        />
        <SelectionRule box={VALUE_PLOT} count={count} selected={selected} />
        <HitTargets
          box={VALUE_PLOT}
          count={count}
          selected={selected}
          onSelect={onSelect}
          labelFor={(index) => reviewReadout(points[index], bucket)}
        />
        <XTicks box={VALUE_PLOT} labels={points.map((point) => formatDay(point.bucket))} />
      </svg>
      <Legend>
        <LegendKey
          label="hours a review waited for an answer"
          swatch={<rect y={5} width={12} height={2} className="fill-amber-300" />}
        />
        <LegendKey
          label="hours a question waited, dashed"
          swatch={
            <>
              <rect y={5} width={5} height={2} className="fill-sky-300" />
              <rect x={8} y={5} width={4} height={2} className="fill-sky-300" />
            </>
          }
        />
      </Legend>
      <SeriesTable
        testId="review-series"
        caption="Review and question waits per bucket, with the first-time approval rate"
        columns={["Bucket", "Answered", "Wait p50", "Wait p90", "First time", "Questions", "Answer p50"]}
        rows={points.map((point) => [
          formatBucket(point.bucket, bucket),
          String(point.exits ?? 0),
          (point.exits ?? 0) > 0 ? hours(point.wait_p50_hours) : "not measured",
          (point.exits ?? 0) > 0 ? hours(point.wait_p90_hours) : "not measured",
          (point.approvals ?? 0) > 0
            ? `${point.first_time_approvals ?? 0} of ${point.approvals}`
            : "—",
          String(point.questions ?? 0),
          (point.answered ?? 0) > 0 ? hours(point.answer_p50_hours) : "not measured",
        ])}
      />
    </figure>
  );
}

// ---------------------------------------------------------------------------
// S4, F5, G4 -- what one completed task cost
// ---------------------------------------------------------------------------

/** One of the three small charts, drawn in {@link SMALL_VIEWBOX}. */
function CostBars({
  points,
  bucket,
  values,
  label,
  fill,
  testId,
  format,
  selected,
  onSelect,
}: {
  points: readonly CostPerTaskPoint[];
  bucket: AnalyticsRange["bucket"];
  values: ReadonlyArray<number | null>;
  label: string;
  fill: string;
  testId: string;
  format: (value: number) => string;
} & Selectable) {
  const count = points.length;
  const max = axisMax(values);
  const bars = columnBars(
    values.map((value) => value ?? 0),
    max,
    SMALL_PLOT,
  );
  const blanks = unknownBuckets(values);

  return (
    <svg
      viewBox={`0 0 ${SMALL_VIEWBOX.width} ${SMALL_VIEWBOX.height}`}
      className="block w-full max-w-[320px] touch-manipulation"
      role="img"
      tabIndex={0}
      data-testid={testId}
      aria-label={`${label} per completed task, per ${bucket}.`}
      onKeyDown={(event) => keyboardSelect(event, selected, count, onSelect)}
    >
      <YAxis box={SMALL_PLOT} max={max} format={format} />
      <BlankBuckets box={SMALL_PLOT} unknown={blanks} reason="Nothing completed in this bucket." />
      {bars.map((bar, index) =>
        blanks[index] ? null : (
          <rect key={bar.index} {...bar} className={fill} data-testid={`${testId}-bar`} />
        ),
      )}
      <SeriesName box={SMALL_PLOT} label={label} className="fill-dark-muted" />
      <SelectionRule box={SMALL_PLOT} count={count} selected={selected} />
      <HitTargets
        box={SMALL_PLOT}
        count={count}
        selected={selected}
        onSelect={onSelect}
        labelFor={(index) => costReadout(points[index], bucket)}
      />
      <XTicks
        box={SMALL_PLOT}
        labels={points.map((point) => formatDay(point.bucket))}
        viewBox={SMALL_VIEWBOX}
      />
    </svg>
  );
}

/**
 * S4, F5 and G4 as three small bar charts side by side: what one completed task cost.
 *
 * Three charts rather than one with three series, because the units do not compare --
 * runs, finishes and minutes -- and stacking or overlaying them would invite exactly
 * the comparison that is meaningless. They share a readout and a selection, so tapping
 * any of them moves all three: the question is about one bucket, seen three ways.
 */
export function CostPerTaskPanel({
  points,
  bucket,
  selected,
  onSelect,
}: {
  points: readonly CostPerTaskPoint[];
  bucket: AnalyticsRange["bucket"];
} & Selectable) {
  const current = points[Math.min(selected, Math.max(points.length - 1, 0))];
  const blank = (value: number | null | undefined, point: CostPerTaskPoint): number | null =>
    point.sample === 0 || value === null || value === undefined || !Number.isFinite(value)
      ? null
      : value;

  return (
    <figure className="mt-1">
      <Readout testId="cost-readout">{costReadout(current, bucket)}</Readout>
      <div className="grid gap-3 sm:grid-cols-3">
        <CostBars
          points={points}
          bucket={bucket}
          values={points.map((point) => blank(point.runs_mean, point))}
          label="runs"
          fill="fill-sky-400"
          testId="cost-runs-chart"
          format={(value) => value.toFixed(1)}
          selected={selected}
          onSelect={onSelect}
        />
        <CostBars
          points={points}
          bucket={bucket}
          values={points.map((point) => blank(point.finishes_mean, point))}
          label="finishes"
          fill="fill-emerald-400"
          testId="cost-finishes-chart"
          format={(value) => value.toFixed(1)}
          selected={selected}
          onSelect={onSelect}
        />
        <CostBars
          points={points}
          bucket={bucket}
          values={points.map((point) => blank(point.gate_minutes_p50, point))}
          label="gate minutes"
          fill="fill-violet-300"
          testId="cost-gate-chart"
          format={(value) => minutes(value)}
          selected={selected}
          onSelect={onSelect}
        />
      </div>
      <SeriesTable
        testId="cost-series"
        caption="Runs, finishes and gate minutes per completed task, per bucket"
        columns={["Bucket", "Tasks", "Runs each", "Finishes each", "Gate p50", "Gate p90", "No gate"]}
        rows={points.map((point) => [
          formatBucket(point.bucket, bucket),
          String(point.sample),
          point.sample === 0 ? "—" : (point.runs_mean?.toFixed(1) ?? "—"),
          point.sample === 0 ? "—" : (point.finishes_mean?.toFixed(1) ?? "—"),
          point.sample === 0 ? "—" : minutes(point.gate_minutes_p50),
          point.sample === 0 ? "—" : minutes(point.gate_minutes_p90),
          String(point.without_gate ?? 0),
        ])}
      />
      <p className="mt-1 text-xs text-dark-muted">
        {counted(points.reduce((sum, point) => sum + point.sample, 0), "completed task")} in this
        window.
      </p>
    </figure>
  );
}
