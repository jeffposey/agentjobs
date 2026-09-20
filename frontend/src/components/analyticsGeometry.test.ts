import { describe, expect, it } from "vitest";

import {
  COUNT_PADDING,
  SMALL_VIEWBOX,
  VALUE_PADDING,
  areaPath,
  axisMax,
  bandCenterX,
  bandWidth,
  bandPath,
  blankSpans,
  columnBars,
  estimatedSpans,
  groupedStacks,
  horizontalBars,
  levelPath,
  levelX,
  linePath,
  nearestIndex,
  pairedBars,
  plotBox,
  runs,
  splitBox,
  stackedAreaPaths,
  stackedBars,
  tickIndices,
  valueY,
  type Box,
} from "./analyticsGeometry";

/**
 * The half of the charts jsdom can genuinely verify (docs/analytics-design.md §10.1).
 *
 * Not one of these tests renders anything, and that is the point: the file under test
 * is where every number a chart draws comes from, so its correctness is a property of
 * arithmetic rather than of a layout engine that reports zeros here. What a browser has
 * to answer instead -- does it fit at 390px, does a tap reach it -- is in
 * `e2e/analytics.spec.ts`.
 *
 * A round box: 100 wide, 100 tall, at the origin, so an expected coordinate can be read
 * as a percentage and a wrong sign is obvious rather than arithmetic.
 */
const BOX: Box = { x: 0, y: 0, width: 100, height: 100 };

describe("plotBox", () => {
  it("leaves the padding outside the plot", () => {
    const plot = plotBox();
    expect(plot.x).toBe(28);
    expect(plot.y).toBe(10);
    expect(plot.width).toBe(400 - 28 - 8);
    expect(plot.height).toBe(166 - 10 - 18);
  });
});

describe("splitBox", () => {
  it("splits one spine into two regions with a gap between them", () => {
    const [top, bottom] = splitBox(BOX, 0.6, 10);
    expect(top).toEqual({ x: 0, y: 0, width: 100, height: 54 });
    expect(bottom).toEqual({ x: 0, y: 64, width: 100, height: 36 });
    // The two regions plus the gap are the original box, exactly: a level and its flows
    // that did not add up would put the bar for a step somewhere other than under it.
    expect(top.height + 10 + bottom.height).toBe(BOX.height);
  });

  it("keeps both regions non-negative when the box is smaller than the gap", () => {
    const [top, bottom] = splitBox({ ...BOX, height: 4 }, 0.5, 10);
    expect(top.height).toBeGreaterThanOrEqual(0);
    expect(bottom.height).toBeGreaterThanOrEqual(0);
  });
});

describe("axisMax", () => {
  it("rounds up to a readable number", () => {
    expect(axisMax([3])).toBe(5);
    expect(axisMax([11])).toBe(20);
    expect(axisMax([126])).toBe(200);
    expect(axisMax([100])).toBe(100);
  });

  it("never returns zero, because an axis of zero divides by zero", () => {
    expect(axisMax([])).toBe(1);
    expect(axisMax([0, 0, 0])).toBe(1);
  });

  it("ignores nulls rather than counting them as zero", () => {
    // §9.3: unknown is never substituted with zero. A null here must not drag the axis
    // down, and must not be read as a data point at the origin either.
    expect(axisMax([null, 8, undefined])).toBe(10);
  });
});

describe("the x spine", () => {
  it("puts one band per bucket", () => {
    expect(bandWidth(4, BOX)).toBe(25);
    expect(bandCenterX(0, 4, BOX)).toBe(12.5);
    expect(bandCenterX(3, 4, BOX)).toBe(87.5);
  });

  it("pushes a level series out to the edges of its box", () => {
    // A level was as true at the left edge of the window as at the centre of its first
    // bucket; half a band of blank at each end reads as missing data.
    expect(levelX(4, BOX)).toEqual([0, 37.5, 62.5, 100]);
  });

  it("bars and the level over them agree about the middle buckets", () => {
    const spine = levelX(5, BOX);
    expect(spine[2]).toBe(bandCenterX(2, 5, BOX));
  });
});

describe("valueY", () => {
  it("measures down from the top of the box", () => {
    expect(valueY(0, 10, BOX)).toBe(100);
    expect(valueY(10, 10, BOX)).toBe(0);
    expect(valueY(5, 10, BOX)).toBe(50);
  });

  it("survives a zero maximum instead of producing NaN", () => {
    expect(valueY(0, 0, BOX)).toBe(100);
  });
});

describe("areaPath", () => {
  it("closes the area down to the zero line", () => {
    const path = areaPath([5, 10], 10, BOX);
    expect(path).toBe("M 0,50 L 100,0 L 100,100 L 0,100 Z");
  });

  it("is empty for an empty series rather than a degenerate shape", () => {
    expect(areaPath([], 10, BOX)).toBe("");
  });

  it("draws its upper boundary at the same points", () => {
    expect(levelPath([5, 10], 10, BOX)).toBe("M 0,50 L 100,0");
  });
});

describe("linePath", () => {
  it("breaks at an unknown value instead of interpolating across it", () => {
    // §8.4: a percentile from two completions is not a number, and a line drawn
    // through the gap would be a trend nobody measured.
    const path = linePath([2, null, 4], 4, BOX);
    expect(path).toBe("M 16.67,50 M 83.33,0");
    expect(path.split("M").length - 1).toBe(2);
  });

  it("is one subpath when nothing is missing", () => {
    expect(linePath([1, 2], 2, BOX).split("M").length - 1).toBe(1);
  });
});

describe("runs", () => {
  it("finds every contiguous run, including one that reaches the end", () => {
    expect(runs([true, true, false, true])).toEqual([
      [0, 1],
      [3, 3],
    ]);
    expect(runs([false, false])).toEqual([]);
  });
});

describe("bandPath", () => {
  it("draws only where both bounds are known", () => {
    const path = bandPath([1, null, 1], [3, 4, 3], 4, BOX);
    expect(path.split("M").length - 1).toBe(2);
    expect(path.endsWith("Z")).toBe(true);
  });

  it("is empty when no bucket has both bounds", () => {
    expect(bandPath([null, null], [1, 2], 2, BOX)).toBe("");
  });
});

describe("columnBars", () => {
  it("grows each bar up from the bottom of the box", () => {
    const bars = columnBars([5, 10], 10, BOX);
    expect(bars[0]).toEqual({ index: 0, x: 7.5, y: 50, width: 35, height: 50 });
    expect(bars[1]?.height).toBe(100);
  });
});

describe("stackedBars", () => {
  it("puts the second segment on top of the first", () => {
    const [lower, upper] = stackedBars(
      [
        [2, 0],
        [1, 3],
      ],
      4,
      BOX,
    );
    expect(lower?.[0]?.y).toBe(50);
    expect(lower?.[0]?.height).toBe(50);
    // The cancelled segment sits on the completed one, so the bar's total is the sum.
    expect(upper?.[0]?.y).toBe(25);
    expect(upper?.[0]?.height).toBe(25);
    // A bucket whose lower segment is empty still stacks from the real zero line.
    expect(upper?.[1]?.height).toBe(75);
  });
});

describe("pairedBars", () => {
  it("scales both halves the same way", () => {
    // Two independently scaled halves would answer "did more arrive than left" wrongly
    // at a glance every time the two differed.
    const { up, down, zeroY } = pairedBars([4], [2], 4, BOX);
    expect(zeroY).toBe(50);
    expect(up[0]?.height).toBe(50);
    expect(down[0]?.height).toBe(25);
    expect(up[0]?.y).toBe(0);
    expect(down[0]?.y).toBe(50);
  });

  it("pairs buckets even when one flow is shorter", () => {
    const { up, down } = pairedBars([1, 2], [1], 2, BOX);
    expect(up).toHaveLength(2);
    expect(down).toHaveLength(2);
    expect(down[1]?.height).toBe(0);
  });
});

describe("horizontalBars", () => {
  it("runs left to right, one row per band", () => {
    const bars = horizontalBars([10, 5], 10, BOX);
    expect(bars[0]?.width).toBe(100);
    expect(bars[1]?.width).toBe(50);
    expect(bars[1]?.y).toBeGreaterThan(bars[0]?.y ?? 0);
  });
});

describe("stackedAreaPaths", () => {
  it("returns one path per band, stacked in order", () => {
    const paths = stackedAreaPaths(
      [
        [1, 1],
        [1, 3],
      ],
      4,
      BOX,
    );
    expect(paths).toHaveLength(2);
    // The lower band's top edge is the upper band's bottom edge: 1 of 4 is 75 down.
    expect(paths[0]?.startsWith("M 0,75 L 100,75")).toBe(true);
    expect(paths[1]?.startsWith("M 0,50 L 100,0")).toBe(true);
    expect(paths[1]?.endsWith("Z")).toBe(true);
  });

  it("returns an empty path per band when there is nothing to stack", () => {
    expect(stackedAreaPaths([[], []], 1, BOX)).toEqual(["", ""]);
  });
});

describe("tickIndices", () => {
  it("never labels more than six buckets", () => {
    expect(tickIndices(90)).toEqual([0, 15, 30, 45, 60, 75]);
    expect(tickIndices(200).length).toBeLessThanOrEqual(6);
  });

  it("labels every bucket when there are few", () => {
    expect(tickIndices(4)).toEqual([0, 1, 2, 3]);
    expect(tickIndices(0)).toEqual([]);
  });
});

describe("nearestIndex", () => {
  it("selects the bucket the tap landed in", () => {
    expect(nearestIndex(0, 4)).toBe(0);
    expect(nearestIndex(0.6, 4)).toBe(2);
    expect(nearestIndex(0.99, 4)).toBe(3);
  });

  it("clamps a tap outside the plot rather than indexing off the end", () => {
    expect(nearestIndex(1.4, 4)).toBe(3);
    expect(nearestIndex(-0.2, 4)).toBe(0);
    expect(nearestIndex(0.5, 0)).toBe(0);
  });
});

describe("estimatedSpans", () => {
  it("hatches each run of reconstructed buckets, not a guessed prefix", () => {
    // This project's own reconstructed span is twenty-five scattered days, so a single
    // hatched prefix would be a claim about where it ended (§3.6 rule 2).
    const spans = estimatedSpans([true, false, true, true], BOX);
    expect(spans).toHaveLength(2);
    expect(spans[0]).toEqual({ x: 0, y: 0, width: 25, height: 100 });
    expect(spans[1]).toEqual({ x: 50, y: 0, width: 50, height: 100 });
  });

  it("hatches nothing when the whole window is exact", () => {
    expect(estimatedSpans([false, false], BOX)).toEqual([]);
  });
});

describe("blankSpans", () => {
  it("marks each run of buckets a series has no number for", () => {
    // §9.3, and deliberately a second name for the same run-finding: a bucket the
    // store cannot place exactly and a bucket it cannot speak for at all are drawn
    // differently and mean different things, so the two call sites read differently.
    expect(blankSpans([false, true, true, false], BOX)).toEqual([
      { x: 25, y: 0, width: 50, height: 100 },
    ]);
  });

  it("marks nothing where every bucket has a value", () => {
    expect(blankSpans([false, false], BOX)).toEqual([]);
  });
});

describe("the second set's paddings", () => {
  it("leaves room on the left for an hour label and above the plot for a series name", () => {
    // §19.2: no axis label sits inside the plot area, so the name goes in the top
    // margin -- which only exists if the padding makes one. `300 h` does not fit in
    // the 28 units a three-digit count needed.
    expect(VALUE_PADDING.left).toBeGreaterThan(COUNT_PADDING.left);
    for (const padding of [VALUE_PADDING, COUNT_PADDING]) {
      expect(padding.top, "a 12-unit label needs more than 12 units above the plot").toBeGreaterThan(12);
    }
  });

  it("gives the small cost charts a squarer box than the wide ones", () => {
    // Three of them sit in a row on a wide screen, so each is about a third of the
    // width; at 2.4:1 that would be a letterbox.
    expect(SMALL_VIEWBOX.width / SMALL_VIEWBOX.height).toBeLessThan(2);
  });
});

describe("groupedStacks", () => {
  const GROUPS = [[[10, 0]], [[2, 4], [1, 0]]];

  it("puts each group in its own slot of the band and scales it on its own max", () => {
    const [runs, hours] = groupedStacks(GROUPS, [10, 5], BOX);
    const firstRun = runs?.[0]?.[0];
    const firstHour = hours?.[0]?.[0];
    // Two groups, so each takes half of the 70% of the band that bars fill: 35 units
    // of a 50-unit band, the first starting 7.5 in from the band's left edge.
    expect(firstRun?.width).toBe(17.5);
    expect(firstRun?.x).toBe(7.5);
    expect(firstHour?.x).toBe(25);
    // 10 of a max of 10 is the full height; 2 of a max of 5 is two fifths of it.
    expect(firstRun?.height).toBe(100);
    expect(firstHour?.height).toBe(40);
  });

  it("stacks the series inside a group on the group's own scale", () => {
    const [, hours] = groupedStacks(GROUPS, [10, 5], BOX);
    const base = hours?.[0]?.[0];
    const above = hours?.[1]?.[0];
    expect(above?.x).toBe(base?.x);
    // The paused hours sit on top of the agent hours: 1 of 5 is a fifth, and its
    // bottom edge is the top of the bar below it.
    expect(above?.height).toBe(20);
    expect((above?.y ?? 0) + (above?.height ?? 0)).toBe(base?.y);
  });

  it("gives a bucket nobody has a value for a bar of no height, not a negative one", () => {
    const [runs] = groupedStacks([[[0, 0]]], [1], BOX);
    expect(runs?.[0]?.every((bar) => bar.height === 0)).toBe(true);
  });
});
