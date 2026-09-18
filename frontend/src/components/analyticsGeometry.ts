/**
 * The maths behind every chart on the analytics page, as functions with no DOM in them.
 *
 * `docs/analytics-design.md` §10.1 records the measurement this file exists because of:
 * this repository's jsdom (30.0.1) returns zeros from `getBoundingClientRect`, has no
 * `ResizeObserver`, no `SVGSVGElement.prototype.getBBox` and a null 2d canvas context.
 * Anything that sizes itself from the DOM therefore renders nothing in the `vitest`
 * stage, and a shim that makes it appear to work is the trap ENGINEERING.md names --
 * *do not set up the state your test is meant to be checking*.
 *
 * So nothing here measures anything. Every function is `(data, box) => geometry`, the
 * box is a fixed viewBox rectangle chosen by the caller, and the browser does the only
 * scaling that happens by drawing that viewBox into whatever width it has. That is what
 * makes these functions testable with no DOM whatsoever, and it is why they are written
 * and tested **before** any component: it is the half of the chart jsdom can genuinely
 * verify, and writing it first is what keeps the tests honest.
 */

/** A rectangle in viewBox coordinates. SVG's y grows downward, as everywhere here. */
export interface Box {
  x: number;
  y: number;
  width: number;
  height: number;
}

/**
 * The coordinate system every chart is drawn in, regardless of the pixels it lands in.
 *
 * **The ratio decides the height and the width decides the type size**, and both
 * numbers are chosen for that rather than for the drawing.
 *
 * 2.4:1, because an inline `<svg>` with a viewBox and `width: 100%` takes its height
 * from its ratio: the chart is about 136px tall in a phone's column, 200px in a
 * tablet's and 245px in a desktop panel, which is §10.2's 160/220/260 arrived at by
 * proportion rather than by three hard-coded heights and with no dead space at any of
 * them. `preserveAspectRatio` keeps its default, so nothing is ever stretched and the
 * chart cannot overflow its column -- half of "nothing scrolls horizontally" (§10.4).
 *
 * 400 units wide, because §10.2's other claim only holds below the column width: text
 * inside the viewBox is a fixed size in *viewBox units*, so a small screen gets
 * proportionally larger type only where the viewBox is being scaled up. A phone's
 * column is about 330px inside a panel, so a 600-unit viewBox would shrink every axis
 * label by a third on exactly the screen that can least afford it. At 400 the phone
 * scales it by ~0.8 and a desktop panel by ~1.4, which is the direction §10.2 wants.
 */
export const VIEWBOX = { width: 400, height: 166 } as const;

/** Room inside the viewBox for the axis labels, which are drawn outside the plot. */
export const PADDING = { left: 28, right: 8, top: 10, bottom: 18 } as const;

/** The plot rectangle: the viewBox less its padding. */
export function plotBox(padding = PADDING, box = VIEWBOX): Box {
  return {
    x: padding.left,
    y: padding.top,
    width: box.width - padding.left - padding.right,
    height: box.height - padding.top - padding.bottom,
  };
}

/**
 * Cut a box into an upper and a lower region sharing one x spine.
 *
 * The backlog chart is "one chart, two layers" (§8.3) -- a level above, its flows
 * below -- and they must not be two charts, because a reader comparing a step in the
 * level with the bar that caused it should not have to align two x axes by eye.
 */
export function splitBox(box: Box, topFraction: number, gap = 6): [Box, Box] {
  const usable = box.height - gap;
  const top = Math.max(0, usable * topFraction);
  return [
    { x: box.x, y: box.y, width: box.width, height: top },
    { x: box.x, y: box.y + top + gap, width: box.width, height: Math.max(0, usable - top) },
  ];
}

/**
 * A round number at or above the largest value, never zero.
 *
 * Zero would divide by zero in {@link valueY}, and an axis top of zero is also the one
 * case where "unknown" and "nothing" would look identical -- so an all-empty series
 * gets an axis of 1 and draws a flat line along a real zero, which is a fact about the
 * data rather than a failure to draw it. `null` values are ignored rather than counted
 * as zero (§9.3: never substitute zero for unknown).
 */
export function axisMax(values: ReadonlyArray<number | null | undefined>, minimum = 1): number {
  let peak = 0;
  for (const value of values) {
    if (value === null || value === undefined || !Number.isFinite(value)) continue;
    if (value > peak) peak = value;
  }
  if (peak <= 0) return minimum;
  const magnitude = 10 ** Math.floor(Math.log10(peak));
  for (const step of [1, 2, 2.5, 5, 10]) {
    const candidate = step * magnitude;
    if (peak <= candidate) return candidate;
  }
  return 10 * magnitude;
}

/** The width one bucket occupies, including the gap that separates it from the next. */
export function bandWidth(count: number, box: Box): number {
  return count > 0 ? box.width / count : box.width;
}

/** The left edge of bucket `index`, which is also the left edge of its hit target. */
export function bandX(index: number, count: number, box: Box): number {
  return box.x + bandWidth(count, box) * index;
}

/** The x every series puts bucket `index` on. */
export function bandCenterX(index: number, count: number, box: Box): number {
  return bandX(index, count, box) + bandWidth(count, box) / 2;
}

/** Where a value sits vertically in a zero-based box. */
export function valueY(value: number, max: number, box: Box): number {
  const safe = max > 0 ? max : 1;
  const clamped = Math.min(Math.max(value, 0), safe);
  return box.y + box.height * (1 - clamped / safe);
}

function round(value: number): number {
  return Math.round(value * 100) / 100;
}

/**
 * The x each point of a level series is drawn at.
 *
 * Band centres, except that the first and last are pushed out to the edges of the box:
 * a level is a continuous quantity that was as true at the left edge of the window as
 * at the centre of its first bucket, and stopping half a band short of each edge reads
 * as missing data rather than as a margin. Overlays on top of bars -- the cycle-time
 * line, its band -- deliberately do *not* do this, so they stay above their own bars.
 */
export function levelX(count: number, box: Box): number[] {
  if (count <= 0) return [];
  if (count === 1) return [box.x];
  const xs = Array.from({ length: count }, (_, index) => bandCenterX(index, count, box));
  xs[0] = box.x;
  xs[count - 1] = box.x + box.width;
  return xs;
}

/** A filled area from the box's zero line up to `values`. */
export function areaPath(values: readonly number[], max: number, box: Box): string {
  if (values.length === 0) return "";
  const xs = levelX(values.length, box);
  const bottom = round(box.y + box.height);
  const points = values.map((value, index) => `${round(xs[index] ?? 0)},${round(valueY(value, max, box))}`);
  return `M ${points.join(" L ")} L ${round(xs[values.length - 1] ?? 0)},${bottom} L ${round(xs[0] ?? 0)},${bottom} Z`;
}

/** The upper boundary of the same area, as a stroke. */
export function levelPath(values: readonly number[], max: number, box: Box): string {
  if (values.length === 0) return "";
  const xs = levelX(values.length, box);
  return `M ${values
    .map((value, index) => `${round(xs[index] ?? 0)},${round(valueY(value, max, box))}`)
    .join(" L ")}`;
}

/**
 * A line over bucketed bars, broken wherever the value is unknown.
 *
 * The gap is the point (§8.4): a percentile computed from two completions is not a
 * number, and interpolating across it would draw a trend nobody measured. Each run of
 * known values becomes its own subpath, and a lone known value becomes a one-point
 * subpath that the caller renders as a dot.
 */
export function linePath(values: ReadonlyArray<number | null>, max: number, box: Box): string {
  const count = values.length;
  const parts: string[] = [];
  let run: string[] = [];
  const flush = () => {
    if (run.length > 0) parts.push(`M ${run.join(" L ")}`);
    run = [];
  };
  values.forEach((value, index) => {
    if (value === null || !Number.isFinite(value)) {
      flush();
      return;
    }
    run.push(`${round(bandCenterX(index, count, box))},${round(valueY(value, max, box))}`);
  });
  flush();
  return parts.join(" ");
}

/** Which runs of buckets have both bounds, as `[first, last]` index pairs. */
export function runs(present: readonly boolean[]): Array<[number, number]> {
  const found: Array<[number, number]> = [];
  let start: number | null = null;
  present.forEach((flag, index) => {
    if (flag && start === null) start = index;
    if (!flag && start !== null) {
      found.push([start, index - 1]);
      start = null;
    }
  });
  if (start !== null) found.push([start, present.length - 1]);
  return found;
}

/**
 * The band between two series, drawn only where both are known.
 *
 * One subpath per run, for the same reason {@link linePath} breaks: a p50-to-p90 band
 * closed across a bucket nobody could measure would claim a spread that was never
 * computed.
 */
export function bandPath(
  lower: ReadonlyArray<number | null>,
  upper: ReadonlyArray<number | null>,
  max: number,
  box: Box,
): string {
  const count = Math.min(lower.length, upper.length);
  const known = Array.from({ length: count }, (_, index) => {
    const low = lower[index];
    const high = upper[index];
    return low !== null && low !== undefined && high !== null && high !== undefined;
  });
  const parts: string[] = [];
  for (const [from, to] of runs(known)) {
    const top: string[] = [];
    const bottom: string[] = [];
    for (let index = from; index <= to; index += 1) {
      const x = round(bandCenterX(index, count, box));
      top.push(`${x},${round(valueY(upper[index] as number, max, box))}`);
      bottom.unshift(`${x},${round(valueY(lower[index] as number, max, box))}`);
    }
    parts.push(`M ${top.join(" L ")} L ${bottom.join(" L ")} Z`);
  }
  return parts.join(" ");
}

/** A bar's rectangle, with the bucket it belongs to so a component can key on it. */
export interface BarRect extends Box {
  index: number;
}

/** How much of a band a bar fills, leaving the rest as the gap between bars. */
const BAR_FILL = 0.7;

function barMetrics(count: number, box: Box): { width: number; inset: number } {
  const band = bandWidth(count, box);
  const width = Math.max(band * BAR_FILL, 0.5);
  return { width, inset: (band - width) / 2 };
}

/** Bars growing up from the bottom of the box. */
export function columnBars(values: readonly number[], max: number, box: Box): BarRect[] {
  const { width, inset } = barMetrics(values.length, box);
  const bottom = box.y + box.height;
  return values.map((value, index) => {
    const y = valueY(value, max, box);
    return {
      index,
      x: round(bandX(index, values.length, box) + inset),
      y: round(y),
      width: round(width),
      height: round(bottom - y),
    };
  });
}

/**
 * Bars stacked in the order given, bottom segment first.
 *
 * Used for completions with cancellations on top (§8.4): closed-not-completed is not
 * throughput, but hiding it entirely would make a month of cancellations look like a
 * quiet month, so it is a segment of its own rather than either a silent addition or
 * an omission.
 */
export function stackedBars(
  series: ReadonlyArray<readonly number[]>,
  max: number,
  box: Box,
): BarRect[][] {
  const count = series.reduce((widest, one) => Math.max(widest, one.length), 0);
  const { width, inset } = barMetrics(count, box);
  const running: number[] = Array.from({ length: count }, () => 0);
  return series.map((one) =>
    Array.from({ length: count }, (_, index) => {
      const value = one[index] ?? 0;
      const base = running[index] ?? 0;
      const top = base + value;
      running[index] = top;
      const y = valueY(top, max, box);
      return {
        index,
        x: round(bandX(index, count, box) + inset),
        y: round(y),
        width: round(width),
        height: round(valueY(base, max, box) - y),
      };
    }),
  );
}

/** Arrivals above a zero line and departures below it, on one shared scale. */
export interface PairedBars {
  up: BarRect[];
  down: BarRect[];
  zeroY: number;
}

/**
 * Arrivals up, completions down, from a zero line through the middle of the box (§8.3).
 *
 * One scale for both halves, not two: the question the bars answer is whether more
 * arrived than left, and two independently scaled halves would answer it wrongly at a
 * glance every time the two differed.
 */
export function pairedBars(
  up: readonly number[],
  down: readonly number[],
  max: number,
  box: Box,
): PairedBars {
  const count = Math.max(up.length, down.length);
  const { width, inset } = barMetrics(count, box);
  const half = box.height / 2;
  const zeroY = box.y + half;
  const safe = max > 0 ? max : 1;
  const bar = (value: number, index: number, upward: boolean): BarRect => {
    const height = Math.min(Math.max(value, 0), safe) * (half / safe);
    return {
      index,
      x: round(bandX(index, count, box) + inset),
      y: round(upward ? zeroY - height : zeroY),
      width: round(width),
      height: round(height),
    };
  };
  return {
    up: Array.from({ length: count }, (_, index) => bar(up[index] ?? 0, index, true)),
    down: Array.from({ length: count }, (_, index) => bar(down[index] ?? 0, index, false)),
    zeroY: round(zeroY),
  };
}

/**
 * Bars running left to right, one per row (§8.5).
 *
 * Horizontal because the age bands are labelled with words -- `7-29d`, `90d+` -- and a
 * vertical axis would have to rotate them.
 */
export function horizontalBars(values: readonly number[], max: number, box: Box): BarRect[] {
  const count = values.length;
  const row = count > 0 ? box.height / count : box.height;
  const height = Math.max(row * BAR_FILL, 0.5);
  const inset = (row - height) / 2;
  const safe = max > 0 ? max : 1;
  return values.map((value, index) => ({
    index,
    x: round(box.x),
    y: round(box.y + row * index + inset),
    width: round((Math.min(Math.max(value, 0), safe) / safe) * box.width),
    height: round(height),
  }));
}

/**
 * One filled path per band of a stack, bottom band first (§8.6).
 *
 * The stack's total is what the axis is scaled to, so a rising band is read against the
 * whole rather than against itself. Every band is drawn even when it is empty for the
 * whole window, so its label and its pattern stay in the legend and a reader is not
 * left wondering whether `external` is zero or missing.
 */
export function stackedAreaPaths(
  series: ReadonlyArray<readonly number[]>,
  max: number,
  box: Box,
): string[] {
  const count = series.reduce((widest, one) => Math.max(widest, one.length), 0);
  if (count === 0) return series.map(() => "");
  const xs = levelX(count, box);
  const running: number[] = Array.from({ length: count }, () => 0);
  return series.map((one) => {
    const lower = running.slice();
    const upper = Array.from({ length: count }, (_, index) => (running[index] ?? 0) + (one[index] ?? 0));
    upper.forEach((value, index) => {
      running[index] = value;
    });
    const top = upper.map(
      (value, index) => `${round(xs[index] ?? 0)},${round(valueY(value, max, box))}`,
    );
    const bottom = lower
      .map((value, index) => `${round(xs[index] ?? 0)},${round(valueY(value, max, box))}`)
      .reverse();
    return `M ${top.join(" L ")} L ${bottom.join(" L ")} Z`;
  });
}

/**
 * Which buckets get an x label: at most six, chosen from the bucket count (§10.2).
 *
 * From the data, never from pixels -- there is nothing to measure in the test
 * environment, and a tick density derived from a container width would be the one
 * decision on this page that only a browser could make.
 */
export function tickIndices(count: number, most = 6): number[] {
  if (count <= 0) return [];
  const step = Math.max(1, Math.ceil(count / most));
  const indices: number[] = [];
  for (let index = 0; index < count; index += step) indices.push(index);
  return indices;
}

/** The bucket a tap at `fraction` across the plot selects (§10.4). */
export function nearestIndex(fraction: number, count: number): number {
  if (count <= 0) return 0;
  const index = Math.floor(fraction * count);
  return Math.min(Math.max(index, 0), count - 1);
}

/**
 * The rectangles that hatch the buckets the store cannot place exactly (§3.6 rule 2).
 *
 * One rectangle per contiguous run rather than one per bucket, because the
 * reconstructed span of a real store is not a prefix -- in this project's own history
 * it is twenty-five scattered days -- and a single hatched prefix would be a guess
 * about where it ended.
 */
export function estimatedSpans(estimated: readonly boolean[], box: Box): Box[] {
  const count = estimated.length;
  return runs(estimated).map(([from, to]) => ({
    x: round(bandX(from, count, box)),
    y: round(box.y),
    width: round(bandWidth(count, box) * (to - from + 1)),
    height: round(box.height),
  }));
}
