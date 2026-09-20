import { useId, type KeyboardEvent, type ReactNode } from "react";

import type {
  AgeBucket,
  AnalyticsRange,
  BacklogPoint,
  HolderPoint,
  ThroughputPoint,
} from "../api/types";
import {
  COUNT_PADDING,
  VIEWBOX,
  blankSpans,
  areaPath,
  axisMax,
  bandCenterX,
  bandWidth,
  bandX,
  estimatedSpans,
  horizontalBars,
  levelPath,
  pairedBars,
  plotBox,
  splitBox,
  stackedAreaPaths,
  stackedBars,
  tickIndices,
  valueY,
  type BarRect,
  type Box,
} from "./analyticsGeometry";
import {
  backlogReadout,
  days,
  formatBucket,
  formatDay,
  holderReadout,
  openLevel,
} from "./analyticsSeries";
import { counted, throughputOnlyReadout } from "./analyticsSecondSet";

/**
 * The four chart shapes the analytics page is allowed to draw, as inline SVG.
 *
 * `docs/analytics-design.md` §10.1 is the decision and its measurement: no charting
 * dependency, because every library considered sizes itself from a DOM that reports
 * zeros in this repository's test environment, and a shim that made one appear to work
 * would be a test of the shim. Everything geometric here comes from
 * `analyticsGeometry.ts`, which has no DOM in it at all; these components are the thin
 * layer that turns those numbers into elements.
 *
 * Three rules bind every chart below, and each of them is a property a test can read:
 *
 * - **Nothing is hover-only** (§10.4). Every value is either printed on the chart, in
 *   the readout line above it, or in the visually-hidden table after it.
 * - **Colour is never the only channel** (§10.3). Each series carries a fill pattern
 *   and an inline label as well as a colour.
 * - **Nothing measures itself.** Tap targets are real rectangles, one per bucket, so
 *   hit testing is the browser's job rather than arithmetic on a bounding box that
 *   jsdom would answer with zeros.
 */

/** The plot rectangle every chart on the page shares. */
export const PLOT = plotBox();

/**
 * How a chart fills the column it is in (§10.2).
 *
 * No fixed height: an inline `<svg>` with a viewBox and `width: 100%` takes its height
 * from the viewBox's ratio, which makes the chart taller exactly where there is more
 * room and needs no `ResizeObserver` to do it.
 *
 * **The ceiling is on the width and not on the height**, which was a defect in a
 * browser before it was a decision. A `max-height` does not stop the box being full
 * width, so `preserveAspectRatio` fitted the chart to the height and centred it, and a
 * 1100px desktop panel held a 675px chart with two wedges of empty space beside it.
 * Capping the width caps the height through the same ratio -- 720 units wide is 299
 * tall -- and leaves no dead space at any size, because the box is never a shape the
 * viewBox has to be letterboxed into.
 */
export const SVG_CLASS = "block w-full max-w-[720px] touch-manipulation";

/**
 * Axis and inline-label type size, in viewBox units rather than pixels.
 *
 * 12 units against a 400-unit viewBox: about 10px in a phone's column and about 17px
 * in a desktop panel. Six x-labels of `12 Sep` still fit at the narrow end, which is
 * what {@link tickIndices} is capped for.
 */
export const LABEL = 12;

export interface Selectable {
  /** The bucket the readout is currently describing. */
  selected: number;
  onSelect: (index: number) => void;
}

/**
 * One bucket's share of the plot, as a transparent rectangle covering its full height.
 *
 * Real elements rather than a single overlay whose offset is measured, for two
 * reasons. The hit target is then the whole column without anything computing where
 * the column is (§10.4), and the gesture is testable: `getBoundingClientRect` returns
 * zeros in jsdom, so an overlay that divided a tap's x by a measured width would be
 * untestable here and would silently select bucket zero every time.
 */
export function HitTargets({
  box,
  count,
  labelFor,
  selected,
  onSelect,
}: {
  box: Box;
  count: number;
  labelFor: (index: number) => string;
} & Selectable) {
  if (count === 0) return null;
  const width = bandWidth(count, box);
  return (
    <g>
      {Array.from({ length: count }, (_, index) => (
        <rect
          key={index}
          data-bucket={index}
          x={bandX(index, count, box)}
          y={box.y}
          width={width}
          height={box.height}
          fill="transparent"
          role="button"
          tabIndex={-1}
          aria-label={labelFor(index)}
          aria-pressed={index === selected}
          onClick={() => onSelect(index)}
          onPointerDown={() => onSelect(index)}
        />
      ))}
    </g>
  );
}

/** The rule marking which bucket the readout is about. */
export function SelectionRule({ box, count, selected }: { box: Box; count: number; selected: number }) {
  if (count === 0) return null;
  const x = bandCenterX(Math.min(selected, count - 1), count, box);
  return (
    <line
      x1={x}
      x2={x}
      y1={box.y}
      y2={box.y + box.height}
      className="stroke-dark-text"
      strokeWidth={1}
      strokeDasharray="3 3"
      opacity={0.7}
      data-testid="selection-rule"
    />
  );
}

/** The y axis: a zero line, a top line, and the two numbers that name them. */
export function YAxis({
  box,
  max,
  format = String,
}: {
  box: Box;
  max: number;
  format?: (value: number) => string;
}) {
  return (
    <g>
      <line
        x1={box.x}
        x2={box.x + box.width}
        y1={box.y + box.height}
        y2={box.y + box.height}
        className="stroke-dark-border"
        strokeWidth={1}
      />
      <line
        x1={box.x}
        x2={box.x + box.width}
        y1={box.y}
        y2={box.y}
        className="stroke-dark-border"
        strokeWidth={0.5}
        opacity={0.5}
      />
      <text
        x={box.x - 4}
        y={box.y + 4}
        textAnchor="end"
        fontSize={LABEL}
        className="fill-dark-muted"
      >
        {format(max)}
      </text>
      <text
        x={box.x - 4}
        y={box.y + box.height + 4}
        textAnchor="end"
        fontSize={LABEL}
        className="fill-dark-muted"
      >
        {format(0)}
      </text>
    </g>
  );
}

/**
 * At most six x labels, chosen from the bucket count rather than from a width (§10.2).
 *
 * The first and last are anchored to their own ends so neither can run off the edge of
 * the viewBox, which is the only way a chart could put a horizontal scrollbar on the
 * page.
 */
export function XTicks({
  box,
  labels,
  viewBox = VIEWBOX,
}: {
  box: Box;
  labels: readonly string[];
  /** The viewBox the ticks are being drawn in, when it is not the shared one. */
  viewBox?: { width: number; height: number };
}) {
  const count = labels.length;
  return (
    <g>
      {tickIndices(count).map((index) => {
        const last = index === count - 1;
        return (
          <text
            key={index}
            x={bandCenterX(index, count, box)}
            y={viewBox.height - 6}
            textAnchor={index === 0 ? "start" : last ? "end" : "middle"}
            fontSize={LABEL}
            className="fill-dark-muted"
          >
            {labels[index]}
          </text>
        );
      })}
    </g>
  );
}

/**
 * The hatch over buckets the store cannot place exactly, and the rule at its boundary.
 *
 * §3.6 rule 2: the reader is told on the chart, once, rather than in a document they
 * will not read. The hatch is per bucket, so it lands on the days that were actually
 * reconstructed rather than on a prefix somebody guessed the length of.
 */
export function ReconstructedSpan({
  box,
  estimated,
  patternId,
}: {
  box: Box;
  estimated: readonly boolean[];
  patternId: string;
}) {
  const spans = estimatedSpans(estimated, box);
  if (spans.length === 0) return null;
  const last = spans[spans.length - 1];
  const boundary = last ? last.x + last.width : null;
  return (
    <g data-testid="reconstructed-span">
      {spans.map((span, index) => (
        <rect key={index} {...span} fill={`url(#${patternId})`} />
      ))}
      {boundary !== null && (
        <line
          x1={boundary}
          x2={boundary}
          y1={box.y}
          y2={box.y + box.height}
          className="stroke-amber-300"
          strokeWidth={1}
          opacity={0.8}
        />
      )}
    </g>
  );
}

/**
 * A series' own name, drawn in the padding **above** the plot rather than inside it.
 *
 * §19.2 forbids an axis label inside the plot area, and the first page had one: the
 * cycle-time chart printed *"median cycle, 0-40d"* over its own bars. It is the same
 * §10.3 channel -- a word beside the colour -- moved somewhere it cannot be mistaken
 * for data. `y` is the plot's top less a third of the label height, which is the
 * baseline that leaves the glyphs clear of the axis line.
 */
export function SeriesName({
  box,
  label,
  className,
  anchor = "start",
}: {
  box: Box;
  label: string;
  className: string;
  anchor?: "start" | "end";
}) {
  return (
    <text
      x={anchor === "start" ? box.x : box.x + box.width}
      y={box.y - LABEL / 3}
      textAnchor={anchor}
      fontSize={LABEL}
      className={className}
    >
      {label}
    </text>
  );
}

/**
 * The buckets a series has no number for, marked rather than left as a gap (§9.3).
 *
 * A gap in a bar chart is indistinguishable from a bucket whose value was zero, and
 * that substitution is the one thing §9.3 forbids by name. So an under-sampled bucket
 * gets a faint dashed outline and a title a screen reader can reach; it is visibly
 * blank, which is what §19.2 asks for and not the same as being visibly empty.
 */
export function BlankBuckets({
  box,
  unknown,
  reason,
}: {
  box: Box;
  unknown: readonly boolean[];
  reason: string;
}) {
  const spans = blankSpans(unknown, box);
  if (spans.length === 0) return null;
  return (
    <g data-testid="blank-buckets">
      {spans.map((span, index) => (
        <rect
          key={index}
          {...span}
          fill="none"
          className="stroke-dark-border"
          strokeWidth={0.75}
          strokeDasharray="2 3"
        >
          <title>{reason}</title>
        </rect>
      ))}
    </g>
  );
}

/** The pattern definitions a chart refers to by id. Ids are per instance, not global. */
export function Patterns({ hatch, dots }: { hatch: string; dots: string }) {
  return (
    <defs>
      <pattern
        id={hatch}
        width={6}
        height={6}
        patternTransform="rotate(45)"
        patternUnits="userSpaceOnUse"
      >
        <line
          x1={0}
          y1={0}
          x2={0}
          y2={6}
          className="stroke-dark-muted"
          strokeWidth={1.5}
          opacity={0.45}
        />
      </pattern>
      <pattern id={dots} width={5} height={5} patternUnits="userSpaceOnUse">
        <circle cx={1.5} cy={1.5} r={1} className="fill-dark-text" opacity={0.5} />
      </pattern>
    </defs>
  );
}

/**
 * The visually-hidden table every chart carries (§10.4).
 *
 * Two jobs, and the second is why it is a table rather than a sentence. A screen
 * reader gets the series it cannot see; and a component test reads values out of the
 * DOM instead of parsing path data, which is the difference between asserting what a
 * reader will act on and asserting that a string of coordinates has not changed.
 */
export function SeriesTable({
  caption,
  columns,
  rows,
  testId,
}: {
  caption: string;
  columns: readonly string[];
  rows: ReadonlyArray<readonly string[]>;
  testId: string;
}) {
  // The wrapper is load-bearing, and it was a defect in a browser before it was a
  // decision. `sr-only` is `width: 1px; overflow: hidden`, which a block honours and a
  // `display: table` box does not -- a table takes that width as a minimum and grows to
  // its content, so the hidden series table was 662px wide inside a 390px phone and put
  // a horizontal scrollbar on the whole page. Clipping it inside a block that *does*
  // honour the width is what keeps it invisible to layout as well as to the eye.
  return (
    <div className="sr-only" data-testid={testId}>
      <table>
        <caption>{caption}</caption>
        <thead>
          <tr>
            {columns.map((column) => (
              <th key={column} scope="col">
                {column}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row[0]}>
              {row.map((cell, index) =>
                index === 0 ? (
                  <th key={index} scope="row">
                    {cell}
                  </th>
                ) : (
                  <td key={index}>{cell}</td>
                ),
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** One series' entry in the legend: its colour, its pattern and its name (§10.3). */
export function LegendKey({ label, swatch }: { label: string; swatch: ReactNode }) {
  return (
    <li className="flex items-center gap-1.5">
      <svg viewBox="0 0 12 12" className="h-3 w-3 shrink-0" aria-hidden="true">
        {swatch}
      </svg>
      <span>{label}</span>
    </li>
  );
}

export function Legend({ children }: { children: ReactNode }) {
  return (
    <ul className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-dark-muted">{children}</ul>
  );
}

/**
 * The readout line, above the chart and always present.
 *
 * Above rather than below, because on a phone the chart is what the thumb is on and a
 * line underneath it would be the part covered by the hand that just tapped.
 */
export function Readout({ children, testId }: { children: ReactNode; testId: string }) {
  return (
    <p className="mb-2 font-mono text-xs text-dark-text sm:text-sm" data-testid={testId}>
      {children}
    </p>
  );
}

/** Arrow keys move the selection, so the charts are reachable without a pointer. */
export function keyboardSelect(
  event: KeyboardEvent<SVGSVGElement>,
  selected: number,
  count: number,
  onSelect: (index: number) => void,
) {
  if (count === 0) return;
  const step = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
  if (step === 0) return;
  event.preventDefault();
  onSelect(Math.min(Math.max(selected + step, 0), count - 1));
}

/**
 * Backlog: the open level as a filled area, its flows as paired bars beneath (§8.3).
 *
 * One chart rather than two, on one x spine, because a reader comparing a step in the
 * level with the bar that caused it should not have to align two axes by eye. The
 * level's axis starts at zero: a truncated axis makes a 3% change look like a cliff,
 * and this is the number the project is judged by.
 */
export function BacklogChart({
  points,
  bucket,
  selected,
  onSelect,
}: {
  points: readonly BacklogPoint[];
  bucket: AnalyticsRange["bucket"];
} & Selectable) {
  const ids = useId();
  const hatch = `${ids}-hatch`;
  const dots = `${ids}-dots`;
  const [levelBox, flowBox] = splitBox(PLOT, 0.62);
  const level = openLevel(points);
  const levelMax = axisMax(level.values);
  const opened = points.map((point) => point.opened);
  const closed = points.map((point) => point.closed);
  const flowMax = axisMax([...opened, ...closed]);
  const bars = pairedBars(opened, closed, flowMax, flowBox);
  const count = points.length;
  const current = points[Math.min(selected, Math.max(count - 1, 0))];
  const last = level.values[count - 1] ?? 0;

  return (
    <figure className="mt-1">
      <Readout testId="backlog-readout">
        {current ? backlogReadout(current, bucket) : "No buckets in this window."}
      </Readout>
      <svg
        viewBox={`0 0 ${VIEWBOX.width} ${VIEWBOX.height}`}
        className={SVG_CLASS}
        role="img"
        tabIndex={0}
        data-testid="backlog-chart"
        aria-label={`Open tasks over time, ending at ${last}, with tasks opened and closed in each bucket.`}
        onKeyDown={(event) => keyboardSelect(event, selected, count, onSelect)}
      >
        <Patterns hatch={hatch} dots={dots} />
        <YAxis box={levelBox} max={levelMax} />
        <ReconstructedSpan
          box={PLOT}
          estimated={points.map((point) => point.estimated)}
          patternId={hatch}
        />
        <path
          d={areaPath(level.values, levelMax, levelBox)}
          className="fill-sky-400"
          opacity={0.25}
        />
        <path
          d={levelPath(level.values, levelMax, levelBox)}
          className="stroke-sky-300"
          fill="none"
          strokeWidth={2}
          data-testid="backlog-level"
        />
        <line
          x1={flowBox.x}
          x2={flowBox.x + flowBox.width}
          y1={bars.zeroY}
          y2={bars.zeroY}
          className="stroke-dark-border"
          strokeWidth={1}
        />
        {bars.up.map((bar) => (
          <rect
            key={`o${bar.index}`}
            {...bar}
            className="fill-amber-300"
            data-testid="opened-bar"
          />
        ))}
        {bars.down.map((bar) => (
          <rect
            key={`c${bar.index}`}
            {...bar}
            className="fill-emerald-400"
            data-testid="closed-bar"
          />
        ))}
        {/* The level's own name, at the right-hand end of the line it belongs to
            (§10.3). The two flows carry theirs in the legend instead: a word placed
            inside a band of ninety bars is illegible wherever it is put, and the
            channel that distinguishes them here is their *side of the zero line*,
            which the legend states in words. */}
        <text
          x={PLOT.x + PLOT.width - 2}
          y={valueY(level.values[count - 1] ?? 0, levelMax, levelBox) - 4}
          textAnchor="end"
          fontSize={LABEL}
          className="fill-sky-300"
        >
          open
        </text>
        <SelectionRule box={PLOT} count={count} selected={selected} />
        <HitTargets
          box={PLOT}
          count={count}
          selected={selected}
          onSelect={onSelect}
          labelFor={(index) => {
            const point = points[index];
            return point ? backlogReadout(point, bucket) : "";
          }}
        />
        <XTicks box={PLOT} labels={points.map((point) => formatDay(point.day))} />
      </svg>
      <Legend>
        <LegendKey
          label="open tasks"
          swatch={<rect width={12} height={12} className="fill-sky-400" opacity={0.6} />}
        />
        <LegendKey
          label="opened, above the line"
          swatch={<rect width={12} height={12} className="fill-amber-300" />}
        />
        <LegendKey
          label="closed, below the line"
          swatch={<rect width={12} height={12} className="fill-emerald-400" />}
        />
        {points.some((point) => point.estimated) && (
          <LegendKey
            label="reconstructed"
            swatch={
              <>
                <rect width={12} height={12} className="fill-dark-surface" />
                <line x1={0} y1={12} x2={12} y2={0} className="stroke-dark-muted" strokeWidth={2} />
              </>
            }
          />
        )}
      </Legend>
      <SeriesTable
        testId="backlog-series"
        caption="Open tasks, arrivals and departures per bucket"
        columns={["Bucket", "Open", "Opened", "Closed", "Exact"]}
        rows={points.map((point, index) => [
          formatBucket(point.day, bucket),
          String(level.values[index] ?? 0),
          String(point.opened),
          String(point.closed),
          point.estimated ? "reconstructed" : "yes",
        ])}
      />
    </figure>
  );
}

/**
 * Throughput: completions per bucket, its own axis, nothing else on it (§19.2).
 *
 * **What was removed is the point.** This chart used to carry a cycle-time line and a
 * p50-to-p90 band on a second axis, and the owner reported that he could not read it.
 * Two axes was defensible when the question was "are we finishing faster" and neither
 * series answered it alone; §18.1 split that question into where a task's time
 * actually goes, which is its own panel, and what is left here is a count. A count
 * needs one axis.
 *
 * Two smaller changes follow from it. The grain is the spine's, so a 30-day range is
 * completed-per-day -- the owner's own ask, and possible only because no percentile is
 * being computed per bucket any more. And **no label sits inside the plot area**: the
 * series names itself above the plot, in the padding, so the only marks inside the
 * rectangle are the data.
 */
export function ThroughputChart({
  points,
  bucket,
  selected,
  onSelect,
}: {
  points: readonly ThroughputPoint[];
  bucket: AnalyticsRange["bucket"];
} & Selectable) {
  const ids = useId();
  const hatch = `${ids}-hatch`;
  const dots = `${ids}-dots`;
  const box = plotBox(COUNT_PADDING);
  const completed = points.map((point) => point.tasks_completed);
  const cancelled = points.map((point) => point.cancelled);
  const countMax = axisMax(completed.map((value, index) => value + (cancelled[index] ?? 0)));
  const [completedBars, cancelledBars] = stackedBars([completed, cancelled], countMax, box);
  const count = points.length;
  const current = points[Math.min(selected, Math.max(count - 1, 0))];
  const total = completed.reduce((sum, value) => sum + value, 0);
  const reopened = points.reduce((sum, point) => sum + (point.reopened ?? 0), 0);

  return (
    <figure className="mt-1">
      <Readout testId="throughput-readout">{throughputOnlyReadout(current, bucket)}</Readout>
      <svg
        viewBox={`0 0 ${VIEWBOX.width} ${VIEWBOX.height}`}
        className={SVG_CLASS}
        role="img"
        tabIndex={0}
        data-testid="throughput-chart"
        aria-label={`Tasks completed per ${bucket}, ${total} in this window.`}
        onKeyDown={(event) => keyboardSelect(event, selected, count, onSelect)}
      >
        <Patterns hatch={hatch} dots={dots} />
        <YAxis box={box} max={countMax} />
        <ReconstructedSpan
          box={box}
          estimated={points.map((point) => point.estimated ?? false)}
          patternId={hatch}
        />
        {(completedBars ?? []).map((bar: BarRect) => (
          <rect
            key={`t${bar.index}`}
            {...bar}
            className="fill-emerald-400"
            data-testid="completed-bar"
          />
        ))}
        {(cancelledBars ?? []).map((bar: BarRect) => (
          <g key={`x${bar.index}`}>
            <rect {...bar} className="fill-dark-muted" opacity={0.5} data-testid="cancelled-bar" />
            <rect {...bar} fill={`url(#${dots})`} />
          </g>
        ))}
        {/* T1's reopening marker: a small caret above the bar, because a reopening is
            neither a completion nor a cancellation and adding it to either stack would
            change a count the page is judged by. The number is in the readout. */}
        {points.map((point, index) =>
          (point.reopened ?? 0) > 0 ? (
            <circle
              key={`r${index}`}
              cx={bandCenterX(index, count, box)}
              // Above its own bar rather than at the top of the plot: a marker pinned
              // to the axis reads as a series of its own floating over the chart,
              // which is the thing this panel was rebuilt to stop doing.
              cy={Math.max(
                valueY(
                  point.tasks_completed + point.cancelled,
                  countMax,
                  box,
                ) - 4,
                box.y + 3,
              )}
              r={2.5}
              className="fill-amber-300"
              data-testid="reopened-marker"
            />
          ) : null,
        )}
        <SeriesName box={box} label="tasks completed" className="fill-emerald-400" />
        <SelectionRule box={box} count={count} selected={selected} />
        <HitTargets
          box={box}
          count={count}
          selected={selected}
          onSelect={onSelect}
          labelFor={(index) => throughputOnlyReadout(points[index], bucket)}
        />
        <XTicks box={box} labels={points.map((point) => formatDay(point.bucket))} />
      </svg>
      <Legend>
        <LegendKey
          label="completed"
          swatch={<rect width={12} height={12} className="fill-emerald-400" />}
        />
        <LegendKey
          label="cancelled"
          swatch={
            <>
              <rect width={12} height={12} className="fill-dark-muted" opacity={0.5} />
              <circle cx={4} cy={4} r={1.5} className="fill-dark-text" />
              <circle cx={9} cy={9} r={1.5} className="fill-dark-text" />
            </>
          }
        />
        {reopened > 0 && (
          <LegendKey
            label={`${counted(reopened, "reopening")}, marked above the bar`}
            swatch={<circle cx={6} cy={6} r={3} className="fill-amber-300" />}
          />
        )}
      </Legend>
      <SeriesTable
        testId="throughput-series"
        caption="Completions, cancellations and reopenings per bucket"
        columns={["Bucket", "Completed", "Completion events", "Cancelled", "Reopened"]}
        rows={points.map((point) => [
          formatBucket(point.bucket, bucket),
          String(point.tasks_completed),
          String(point.completion_events),
          String(point.cancelled),
          String(point.reopened ?? 0),
        ])}
      />
    </figure>
  );
}

/**
 * The age distribution, as four horizontal bars (§8.5).
 *
 * Horizontal because the labels are words -- `7-29d`, `90d+` -- and a vertical axis
 * would have to rotate them. Every count is printed at the end of its own bar, so the
 * chart is readable without reference to an axis at all.
 */
export function AgingChart({ buckets }: { buckets: readonly AgeBucket[] }) {
  const values = buckets.map((band) => band.tasks);
  const max = axisMax(values);
  const box: Box = {
    x: 52,
    y: 10,
    width: VIEWBOX.width - 52 - 30,
    height: 120,
  };
  const bars = horizontalBars(values, max, box);
  const total = values.reduce((sum, value) => sum + value, 0);
  const rowHeight = buckets.length > 0 ? box.height / buckets.length : box.height;

  return (
    <figure className="mt-1">
      <svg
        viewBox={`0 0 ${VIEWBOX.width} 140`}
        className="block w-full max-w-[560px]"
        role="img"
        data-testid="aging-chart"
        aria-label={`Age of the ${total} open tasks, in four bands.`}
      >
        {bars.map((bar, index) => {
          const band = buckets[index];
          if (!band) return null;
          return (
            <g key={band.label}>
              <text
                x={box.x - 6}
                y={box.y + rowHeight * index + rowHeight / 2 + 4}
                textAnchor="end"
                fontSize={LABEL}
                className="fill-dark-text"
              >
                {band.label}
              </text>
              <rect
                x={bar.x}
                y={bar.y}
                width={Math.max(bar.width, 1)}
                height={bar.height}
                className={AGE_FILL[index] ?? "fill-sky-400"}
                data-testid="age-bar"
              />
              <text
                x={bar.x + Math.max(bar.width, 1) + 5}
                y={bar.y + bar.height / 2 + 4}
                fontSize={LABEL}
                className="fill-dark-text"
              >
                {band.tasks}
              </text>
            </g>
          );
        })}
      </svg>
      <SeriesTable
        testId="aging-series"
        caption="Open tasks by age band, archived tasks excluded"
        columns={["Band", "Tasks", "Mean age"]}
        rows={buckets.map((band) => [band.label, String(band.tasks), days(band.mean_age_days)])}
      />
    </figure>
  );
}

/** One fill per age band, oldest the most alarming. Colour follows the printed count. */
const AGE_FILL = ["fill-emerald-400", "fill-sky-400", "fill-amber-300", "fill-rose-400"];

/** The three bands of the holder stack, bottom first: the app's own vocabulary. */
const BANDS = ["agent", "human", "external"] as const;

/**
 * Who has held the open work, as a stacked area (§8.6).
 *
 * "A rising `human` band is the single most actionable thing on the page and it is
 * invisible in any snapshot" -- which is the whole reason this panel is a series
 * rather than the three numbers above it. The bands are told apart by pattern as well
 * as by colour: solid, 45-degree hatch, dots.
 */
export function HolderChart({
  points,
  bucket,
  selected,
  onSelect,
}: {
  points: readonly HolderPoint[];
  bucket: AnalyticsRange["bucket"];
} & Selectable) {
  const ids = useId();
  const hatch = `${ids}-hatch`;
  const dots = `${ids}-dots`;
  const agent = points.map((point) => point.agent);
  const human = points.map((point) => point.human);
  const external = points.map((point) => point.external);
  const max = axisMax(
    agent.map((value, index) => value + (human[index] ?? 0) + (external[index] ?? 0)),
  );
  const paths = stackedAreaPaths([agent, human, external], max, PLOT);
  const count = points.length;
  const current = points[Math.min(selected, Math.max(count - 1, 0))];
  const fills = ["fill-sky-400", "fill-amber-300", "fill-violet-300"];

  return (
    <figure className="mt-1">
      <Readout testId="holders-readout">
        {current ? holderReadout(current, bucket) : "No buckets in this window."}
      </Readout>
      <svg
        viewBox={`0 0 ${VIEWBOX.width} ${VIEWBOX.height}`}
        className={SVG_CLASS}
        role="img"
        tabIndex={0}
        data-testid="holders-chart"
        aria-label={`Who holds the open work over time, ending at ${current?.agent ?? 0} with an agent, ${
          current?.human ?? 0
        } with a human and ${current?.external ?? 0} outside the project.`}
        onKeyDown={(event) => keyboardSelect(event, selected, count, onSelect)}
      >
        <Patterns hatch={hatch} dots={dots} />
        <YAxis box={PLOT} max={max} />
        {paths.map((path, index) => (
          <g key={index}>
            <path d={path} className={fills[index] ?? "fill-sky-400"} opacity={0.55} />
            {index === 1 && <path d={path} fill={`url(#${hatch})`} />}
            {index === 2 && <path d={path} fill={`url(#${dots})`} />}
          </g>
        ))}
        {/* Each band named at its own right-hand edge (§10.3), at the height it
            actually occupies there, so the stack reads without the legend. A band too
            thin to hold its own word is left to the legend rather than overprinted on
            its neighbour. */}
        {BANDS.map((band, index) => {
          const series = [agent, human, external][index] ?? [];
          const below = [agent, human, external]
            .slice(0, index)
            .reduce((sum, one) => sum + (one[count - 1] ?? 0), 0);
          const own = series[count - 1] ?? 0;
          if (own / (max || 1) < 0.12) return null;
          return (
            <text
              key={band}
              x={PLOT.x + PLOT.width - 2}
              y={valueY(below + own / 2, max, PLOT) + LABEL / 3}
              textAnchor="end"
              fontSize={LABEL}
              className="fill-dark-text"
            >
              {band}
            </text>
          );
        })}
        <SelectionRule box={PLOT} count={count} selected={selected} />
        <HitTargets
          box={PLOT}
          count={count}
          selected={selected}
          onSelect={onSelect}
          labelFor={(index) => {
            const point = points[index];
            return point ? holderReadout(point, bucket) : "";
          }}
        />
        <XTicks box={PLOT} labels={points.map((point) => formatDay(point.day))} />
      </svg>
      <Legend>
        <LegendKey
          label="agent"
          swatch={<rect width={12} height={12} className="fill-sky-400" />}
        />
        <LegendKey
          label="human"
          swatch={
            <>
              <rect width={12} height={12} className="fill-amber-300" opacity={0.6} />
              <line x1={0} y1={12} x2={12} y2={0} className="stroke-dark-text" strokeWidth={2} />
            </>
          }
        />
        <LegendKey
          label="external"
          swatch={
            <>
              <rect width={12} height={12} className="fill-violet-300" opacity={0.6} />
              <circle cx={4} cy={4} r={1.5} className="fill-dark-text" />
              <circle cx={9} cy={9} r={1.5} className="fill-dark-text" />
            </>
          }
        />
      </Legend>
      <SeriesTable
        testId="holders-series"
        caption="Open tasks by who holds them, per bucket"
        columns={["Bucket", "Agent", "Human", "External"]}
        rows={points.map((point) => [
          formatBucket(point.day, bucket),
          String(point.agent),
          String(point.human),
          String(point.external),
        ])}
      />
    </figure>
  );
}
