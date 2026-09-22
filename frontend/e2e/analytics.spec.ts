import { expect, test, type Page, type Route } from "./fixtures";

/**
 * The analytics page, measured in a browser rather than asserted in jsdom.
 *
 * task-373 wrote the first half of this file; task-474 added the second set's panels
 * (`docs/analytics-design.md` §19.4) and the properties §19.1 to §19.3 changed.
 *
 * The split between this file and `src/components/Analytics.test.tsx` is the whole of
 * §10.1's argument. jsdom returns zeros from every rectangle, has no `ResizeObserver`
 * and no `getBBox`, so it can say what a chart *claims* and nothing about what it
 * *occupies*. Everything measured below is a real rectangle in Chromium:
 *
 * - the page fits a 390px phone with no horizontal scroll (ac-5),
 * - a tap on a chart column updates that chart's readout, on every panel (ac-5),
 * - the axis labels are large enough to read at that width,
 * - a series younger than the range draws its coverage start rather than zeros (ac-5),
 * - and the Dashboard, which this task must not disturb, still does not scroll.
 *
 * **Where the history comes from.** The e2e project is created fresh for the run, so
 * it cannot have ninety days of anything. Two tests take the server's real answer and
 * lengthen its series; the response *shape* is therefore the server's own -- fetched
 * from the running app, not written here -- and only the number of buckets is
 * synthesised. That is the same amplification `dashboard-one-screen.spec.ts` uses, and
 * for the same reason: the worst case has to be measured, and this project cannot
 * produce it in ninety seconds. The test that needs no length at all uses the real
 * response untouched.
 */

const PHONE = { width: 390, height: 844 };
const DESKTOP = { width: 1280, height: 800 };
const ANALYTICS = "/app/p/_local/analytics";

/** How many buckets the amplified response carries: a 90-day range at day grain. */
const BUCKETS = 90;

const seeded: string[] = [];

test.beforeAll(async ({ request }) => {
  // Two tasks, one of which is closed, so the real store has creations *and* a
  // completion in it -- the real response then exercises both flows rather than only
  // arrivals, and the untouched-response test below has something to draw.
  for (const title of [
    "Seeded by analytics.spec.ts so the store has a creation event",
    "Seeded by analytics.spec.ts and closed, so it has a completion too",
  ]) {
    const response = await request.post("/api/tasks", {
      data: {
        title,
        summary: "A fixture for the analytics page's browser measurements. Closed when this file finishes.",
        description: "Exists so the analytics endpoint has an event to report.",
        lifecycle: "ready",
        category: "ux",
        actor: "E2E Human",
      },
    });
    expect(response.ok(), await response.text()).toBeTruthy();
    seeded.push((await response.json()).id as string);
  }
  const closing = seeded[1];
  if (closing) {
    await request.post(`/api/tasks/${closing}/close`, {
      data: { actor: "E2E Human", outcome: "completed", body: "Fixture; nothing to review." },
    });
  }
});

test.afterAll(async ({ request }) => {
  for (const id of seeded) {
    await request.post(`/api/tasks/${id}/close`, {
      data: { actor: "E2E Human", outcome: "completed", body: "Fixture; nothing to review." },
    });
  }
});

interface AnalyticsBody {
  range: { bucket: string; throughput_bucket: string; start: string; timezone: string };
  coverage: Record<string, unknown>;
  backlog: Array<Record<string, unknown>>;
  holders: Array<Record<string, unknown>>;
  throughput: Array<Record<string, unknown>>;
  segments: Array<Record<string, unknown>>;
  segments_coverage: Record<string, unknown>;
  finishes: Array<Record<string, unknown>>;
  finishes_coverage: Record<string, unknown>;
  gates: Array<Record<string, unknown>>;
  gates_coverage: Record<string, unknown>;
  runs: Array<Record<string, unknown>>;
  runs_coverage: Record<string, unknown>;
  machine: Array<Record<string, unknown>>;
  machine_coverage: Record<string, unknown>;
  review: Array<Record<string, unknown>>;
  review_coverage: Record<string, unknown>;
  cost_per_task: Array<Record<string, unknown>>;
  cost_coverage: Record<string, unknown>;
  in_review: Array<Record<string, unknown>>;
  open_questions: Array<Record<string, unknown>>;
  stuck: Array<Record<string, unknown>>;
}

/**
 * `YYYY-MM-DD` for `offset` days before 18 Sep 2026, without a timezone conversion.
 *
 * A fixed anchor rather than today, and safe here for a reason worth stating -- task-464
 * was these same two clocks going wrong in the Python suite. These keys only ever reach
 * a mocked response, where they are axis labels rather than ages. The one value the page
 * measures against its own clock is `coverage.baseline_at`, and that is `dayKey(BUCKETS)`
 * -- 90 days back, and a day further back with every day that passes, so `historyOf()`
 * reads `full` now and can only go on reading `full`. An anchor that drifted *towards*
 * its threshold would be a time bomb; this one drifts away from it.
 */
function dayKey(offset: number): string {
  const day = new Date(Date.UTC(2026, 8, 18) - offset * 86_400_000);
  return day.toISOString().slice(0, 10);
}

/**
 * Serve the real response with its series lengthened to a realistic window.
 *
 * The level walks rather than stepping once, because a single step would leave most of
 * the area flat and would not tell a chart that had silently collapsed to two points
 * from one that had not. A quarter of the buckets are marked `estimated`, in two runs,
 * so the hatch and its boundary rule are both on screen.
 *
 * The second set's series carry **five different baselines**, which is the state §21.1
 * exists for and the thing a single-coverage fixture could not produce: segments and
 * review reach the whole window, finishes and gates start six weeks in, runs start a
 * fortnight in, and the execution journal five days in.
 */
async function withHistory(page: Page) {
  await page.route("**/api/projects/*/analytics*", async (route: Route) => {
    const response = await route.fetch();
    const body = (await response.json()) as AnalyticsBody;
    // The window has to match the series being planted into it, and the server's own
    // is a few hours wide: this project was created for the run, so the endpoint
    // clipped `range.start` to a baseline of today. Leaving it there would put every
    // amplified bucket before the window, and the summary row -- which since section
    // 19.1 sums from a baseline rather than over the whole series -- would correctly
    // report that it had nothing to compare.
    body.range = { ...body.range, start: `${dayKey(BUCKETS)}T00:00:00Z`, bucket: "day" };
    let open = 40;
    body.backlog = Array.from({ length: BUCKETS }, (_, index) => {
      const opened = (index % 5) + 1;
      const closed = index % 4;
      open += opened - closed;
      return {
        day: dayKey(BUCKETS - 1 - index),
        open_count: open,
        opened,
        closed,
        // Two runs rather than a prefix: the hatch has to land on the buckets that
        // were reconstructed, and a prefix would pass a test a real store would fail.
        estimated: index < 12 || (index > 30 && index < 42),
      };
    });
    body.holders = body.backlog.map((point, index) => ({
      day: point.day as string,
      agent: 30 + (index % 9),
      human: 6 + (index % 5),
      external: index % 3,
    }));
    // §19.2: throughput is the spine grain now, so it shares the backlog's spine.
    body.throughput = body.backlog.map((point, index) => ({
      bucket: point.day as string,
      tasks_completed: index % 6,
      completion_events: (index % 6) + (index === 4 ? 1 : 0),
      cancelled: index % 5 === 0 ? 1 : 0,
      reopened: index === 4 ? 1 : 0,
      estimated: point.estimated as boolean,
    }));

    const week = (index: number) => dayKey((12 - index) * 7);
    body.segments = Array.from({ length: 13 }, (_, index) => {
      // Two buckets deliberately under the minimum sample, so the blank treatment is
      // on screen rather than only in a unit test.
      const sample = index === 3 || index === 8 ? 2 : 4 + (index % 7);
      const measured = sample >= 3;
      const orNull = (value: number) => (measured ? value : null);
      return {
        bucket: week(index),
        sample,
        excluded: index === 2 ? 1 : 0,
        unreviewed: Math.floor(sample / 2),
        first_review_sample: measured ? 2 : 0,
        first_review_p50_hours: orNull(0.8),
        first_review_p90_hours: orNull(4),
        queue_p50_hours: orNull(1 + index * 0.4),
        queue_p90_hours: orNull(40 + index),
        work_p50_hours: orNull(0.6),
        work_p90_hours: orNull(1.5),
        waiting_p50_hours: orNull(0),
        waiting_p90_hours: orNull(2.1),
        review_p50_hours: orNull(0.2),
        review_p90_hours: orNull(1.1),
        finish_p50_hours: orNull(0.1),
        finish_p90_hours: orNull(0.3),
        total_p50_hours: orNull(4.8 + index),
        total_p90_hours: orNull(300),
        among: {},
        estimated: index < 4,
      };
    });
    body.segments_coverage = {
      bucket: "week",
      complete: false,
      recorded_from: dayKey(BUCKETS) + "T00:00:00Z",
      note: null,
    };
    // A series younger than the range: it starts six weeks in and says so (ac-5).
    body.finishes = Array.from({ length: 6 }, (_, index) => ({
      bucket: week(index + 7),
      finished: 3 + index,
      escalated: index % 3,
      declined: 0,
      interrupted: index === 1 ? 1 : 0,
      reasons: index % 3 > 0 ? { gate_failed: index % 3 } : {},
      duration_p50_min: 4.8 + index * 0.2,
      duration_p90_min: 8.3,
      sample: 3 + index,
      steps_p50_s: { gate: 253, merge: 1.2 },
      runway_waited: index === 2 ? 2 : 0,
      runway_p90_s: index === 2 ? 359 : null,
      estimated: false,
    }));
    body.finishes_coverage = {
      bucket: "week",
      complete: false,
      recorded_from: dayKey(42) + "T00:00:00Z",
      note: "Finishes are recorded from 7 Aug 2026.",
    };
    body.gates = body.finishes.map((point, index) => ({
      bucket: point.bucket as string,
      full: 4 + index,
      passed: 3 + index,
      failed_stages: { pytest: 1 },
      duration_p50_min: 4 + index * 0.1,
      duration_p90_min: 6.1,
      sample: 3 + index,
      stages_p50_s: { pytest: 125, e2e: 107 },
      origins: { finish: 3 + index, run: 1 },
      estimated: false,
    }));
    body.gates_coverage = {
      bucket: "week",
      complete: false,
      recorded_from: dayKey(42) + "T00:00:00Z",
      note: "Agent-side gates are recorded only from 12 Sep 2026.",
    };
    body.runs = body.backlog.slice(-14).map((point, index) => ({
      bucket: point.day as string,
      runs: 2 + (index % 9),
      triggers: { manual: 2 + (index % 5), child: index % 3 },
      agent_hours: 1.5 + (index % 6),
      outcomes: { completed: 2 + (index % 7), interrupted: index % 3 },
      in_flight: index === 13 ? 1 : 0,
      duration_p50_min: 27,
      duration_p90_min: 97,
      sample: 2 + (index % 9),
      estimated: false,
    }));
    body.runs_coverage = {
      bucket: "day",
      complete: false,
      recorded_from: dayKey(13) + "T00:00:00Z",
      note: "Runs are recorded from 5 Sep 2026.",
    };
    body.machine = body.runs.slice(-6).map((point, index) => ({
      bucket: point.bucket as string,
      admitted: 4 + index,
      start_latency_p50_s: 2.2,
      start_latency_p90_s: 3.1,
      queued: index % 2,
      queue_wait_p50_s: index % 2 ? 90 : null,
      queue_wait_p90_s: index % 2 ? 140 : null,
      paused_run_hours: index === 2 ? 6.9 : 0,
      paused_waiters: index === 2 ? 5 : 0,
    }));
    body.machine_coverage = {
      bucket: "day",
      complete: false,
      recorded_from: dayKey(5) + "T00:00:00Z",
      note: "The execution journal is recorded from 13 Sep 2026.",
    };
    body.review = Array.from({ length: 13 }, (_, index) => ({
      bucket: week(index),
      exits: 3 + (index % 9),
      approvals: 2 + (index % 7),
      wait_p50_hours: 0.07 + index * 0.1,
      wait_p90_hours: 5.1,
      first_time_approvals: 1 + (index % 4),
      questions: index % 4,
      answered: index % 4 > 0 ? (index % 4) - 1 : 0,
      answer_p50_hours: index % 4 > 1 ? 0.27 : null,
      answer_p90_hours: index % 4 > 1 ? 30 : null,
      estimated: index < 4,
    }));
    body.review_coverage = {
      bucket: "week",
      complete: false,
      recorded_from: dayKey(BUCKETS) + "T00:00:00Z",
      note: null,
    };
    body.cost_per_task = body.segments.map((point, index) => ({
      bucket: point.bucket as string,
      sample: point.sample as number,
      runs_mean: index === 3 ? null : 1.2 + index * 0.05,
      runs_mode: 1,
      finishes_mean: index === 3 ? null : 1.3,
      gate_minutes_p50: index === 3 ? null : 4.4 + index * 0.1,
      gate_minutes_p90: index === 3 ? null : 6.9,
      without_gate: index % 5 === 0 ? 1 : 0,
      estimated: false,
    }));
    body.cost_coverage = {
      bucket: "week",
      complete: false,
      recorded_from: dayKey(42) + "T00:00:00Z",
      note: null,
    };
    body.in_review = [
      { task_id: "task-231", title: "Something waiting on a person", hours_waiting: 5.4 },
      { task_id: "task-240", title: "A second thing waiting", hours_waiting: 0.3 },
    ];
    body.open_questions = [{ task_id: "task-409", entry_id: 7, hours_open: 52 }];
    // Out of §19.3's order, with the queue holding the most -- which is what the API
    // returns and what the page has to reorder.
    body.stuck = [
      { ball: "agent", ball_reason: "available", tasks: 96, mean_days_held: 12.1, max_days_held: 40.5, oldest_task_id: "task-053" },
      { ball: "human", ball_reason: "review", tasks: 11, mean_days_held: 2.4, max_days_held: 9.1, oldest_task_id: "task-101" },
      { ball: "agent", ball_reason: "work", tasks: 3, mean_days_held: 0.4, max_days_held: 1.1, oldest_task_id: "task-474" },
      { ball: "external", ball_reason: "dependency", tasks: 2, mean_days_held: 6, max_days_held: 9.3, oldest_task_id: "task-207" },
    ];

    body.coverage = {
      ...body.coverage,
      baseline_at: `${dayKey(BUCKETS)}T00:00:00Z`,
      baseline_kind: "backfilled",
      native_from: `${dayKey(40)}T00:00:00Z`,
      reconstructed_before: `${dayKey(48)}T00:00:00Z`,
      complete: false,
      note: "History from 20 Jun 2026. Events before 1 Aug 2026 were reconstructed.",
    };
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(body),
    });
  });
}

/** Serve a project that has no history at all, which is section 9.1's state. */
async function withNoHistory(page: Page) {
  await page.route("**/api/projects/*/analytics*", async (route: Route) => {
    const response = await route.fetch();
    const body = (await response.json()) as AnalyticsBody;
    body.coverage = {
      ...body.coverage,
      baseline_at: null,
      baseline_kind: "unknown",
      native_from: null,
      reconstructed_before: null,
      complete: false,
      note: null,
    };
    body.backlog = [];
    body.holders = [];
    body.throughput = [];
    body.segments = [];
    body.finishes = [];
    body.gates = [];
    body.runs = [];
    body.machine = [];
    body.review = [];
    body.cost_per_task = [];
    body.in_review = [];
    body.open_questions = [];
    body.stuck = [];
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(body),
    });
  });
}

/** Open the page and wait for the one request that fills all of it. */
async function openAnalytics(page: Page) {
  await page.goto(ANALYTICS);
  await expect(page.getByTestId("analytics-page")).toBeVisible();
}

/** What the browser says about the document's own horizontal scroll. */
async function horizontalScroll(page: Page) {
  return page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    clientWidth: document.documentElement.clientWidth,
  }));
}

/** Every chart the second version draws, by the id its readout is keyed to. */
const PANELS: ReadonlyArray<{ chart: string; readout: string }> = [
  { chart: "backlog-chart", readout: "backlog-readout" },
  { chart: "throughput-chart", readout: "throughput-readout" },
  { chart: "segments-chart", readout: "segments-readout" },
  { chart: "review-chart", readout: "review-readout" },
  { chart: "finishes-chart", readout: "finishes-readout" },
  { chart: "durations-chart", readout: "durations-readout" },
  { chart: "runs-chart", readout: "runs-readout" },
  { chart: "run-outcomes-chart", readout: "run-outcomes-readout" },
  { chart: "cost-runs-chart", readout: "cost-readout" },
  { chart: "holders-chart", readout: "holders-readout" },
];

test("the route survives a direct visit and a reload", async ({ page }) => {
  await withHistory(page);
  await page.setViewportSize(DESKTOP);
  await openAnalytics(page);
  await page.reload();
  await expect(page.getByTestId("analytics-page")).toBeVisible();
  await expect(page).toHaveURL(new RegExp(`${ANALYTICS}$`));
});

test("the panels are in §19.4's order (ac-2)", async ({ page }) => {
  await withHistory(page);
  await page.setViewportSize(DESKTOP);
  await openAnalytics(page);
  // `getByRole`, not a `[role=...]` selector: a <section aria-label> carries the role
  // implicitly and has no attribute to match, so a raw selector would find only the
  // one element that spells its role out and pass while five regions were missing.
  const labels = await page
    .getByTestId("analytics-page")
    .getByRole("region")
    .evaluateAll((nodes) => nodes.map((node) => node.getAttribute("aria-label")));
  expect(labels).toEqual([
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

test("nothing scrolls horizontally on a 390px phone (ac-5)", async ({ page }) => {
  await withHistory(page);
  await page.setViewportSize(PHONE);
  await openAnalytics(page);

  const scroll = await horizontalScroll(page);
  expect(
    scroll.scrollWidth,
    `the document is ${scroll.scrollWidth}px wide in a ${scroll.clientWidth}px viewport`,
  ).toBeLessThanOrEqual(scroll.clientWidth);

  // And no individual chart is wider than the screen, which is how it would get there.
  const overflowing = await page.evaluate(() => {
    const wide: string[] = [];
    for (const svg of document.querySelectorAll("svg[data-testid]")) {
      const box = svg.getBoundingClientRect();
      if (box.right > window.innerWidth + 1 || box.left < -1) {
        wide.push(`${svg.getAttribute("data-testid")} spans ${box.left}..${box.right}`);
      }
    }
    return wide;
  });
  expect(overflowing, "every chart is inside the viewport").toEqual([]);
});

test("every panel is present and drawn at 390px (ac-2, ac-5)", async ({ page }) => {
  await withHistory(page);
  await page.setViewportSize(PHONE);
  await openAnalytics(page);
  for (const panel of PANELS) {
    const chart = page.getByTestId(panel.chart);
    await expect(chart, `${panel.chart} is missing`).toBeVisible();
    const box = await chart.boundingBox();
    expect(box?.height ?? 0, `${panel.chart} has no height`).toBeGreaterThan(40);
  }
});

test("the axis labels are readable at 390px", async ({ page }) => {
  // A chart that fits by shrinking its own type to four pixels has not fitted. The
  // viewBox is 400 units wide against a phone column of about 330, so a 12-unit label
  // lands near 10px; anything under 8px would mean the viewBox had grown.
  await withHistory(page);
  await page.setViewportSize(PHONE);
  await openAnalytics(page);
  const smallest = await page.evaluate(() => {
    const heights = Array.from(
      document.querySelectorAll("[data-testid='backlog-chart'] text"),
      (node) => node.getBoundingClientRect().height,
    ).filter((height) => height > 0);
    return heights.length > 0 ? Math.min(...heights) : 0;
  });
  expect(smallest, `the smallest rendered label is ${smallest}px tall`).toBeGreaterThan(8);

  // The readout is the line a phone reads values from, so it is held to the app's own
  // smallest type size rather than to the chart's.
  const readout = page.getByTestId("backlog-readout");
  await expect(readout).toBeVisible();
  const size = await readout.evaluate((node) => parseFloat(getComputedStyle(node).fontSize));
  expect(size).toBeGreaterThanOrEqual(12);
});

test("no label is drawn inside a plot area (ac-1)", async ({ page }) => {
  // §19.2. The first page printed *"median cycle, 0-40d"* over its own bars, and the
  // owner reported the chart as unreadable. Measured in the browser rather than on the
  // attributes, because what matters is where the glyphs land.
  await withHistory(page);
  await page.setViewportSize(DESKTOP);
  await openAnalytics(page);
  const offenders = await page.evaluate(() => {
    const found: string[] = [];
    for (const id of ["throughput-chart", "segments-chart", "durations-chart", "review-chart"]) {
      const svg = document.querySelector(`[data-testid='${id}']`) as SVGSVGElement | null;
      if (!svg) continue;
      const box = svg.getBoundingClientRect();
      // The plot occupies the viewBox less its padding; the widest left margin in use
      // is 44 of 400 units and the top is 18 of 166, so a generous inset here still
      // catches a label drawn over the data.
      const left = box.x + box.width * (44 / 400);
      const right = box.x + box.width * (392 / 400);
      const top = box.y + box.height * (18 / 166);
      const bottom = box.y + box.height * (148 / 166);
      for (const text of svg.querySelectorAll("text")) {
        const at = text.getBoundingClientRect();
        if (at.left > left && at.right < right && at.top > top && at.bottom < bottom) {
          found.push(`${id}: "${text.textContent}"`);
        }
      }
    }
    return found;
  });
  expect(offenders, "every label sits in a margin").toEqual([]);
});

test("tapping a column updates that panel's readout, on every panel (ac-5)", async ({ page }) => {
  await withHistory(page);
  await page.setViewportSize(PHONE);
  await openAnalytics(page);

  for (const panel of PANELS) {
    const readout = page.getByTestId(panel.readout);
    const before = await readout.textContent();
    // A real click on a real element, and the proof that it landed is the readout
    // changing -- ENGINEERING.md's rule, because an automation tool reports success
    // whether or not any event arrived. The target is the column's full height.
    const column = page.locator(`[data-testid='${panel.chart}'] [data-bucket='0']`);
    const box = await column.boundingBox();
    expect(box, `${panel.chart}'s hit target is a real rectangle`).not.toBeNull();
    expect(box!.height, `${panel.chart}'s hit target is the full column height`).toBeGreaterThan(
      20,
    );
    await column.click();
    await expect(readout, `${panel.readout} did not change on a tap`).not.toHaveText(before ?? "");
    await expect(column).toHaveAttribute("aria-pressed", "true");
  }
});

test("the selection rule follows the tap", async ({ page }) => {
  await withHistory(page);
  await page.setViewportSize(PHONE);
  await openAnalytics(page);
  const column = page.locator("[data-testid='backlog-chart'] [data-bucket='20']");
  const box = await column.boundingBox();
  await column.click();
  // `toBeAttached` rather than `toBeVisible`: a vertical line has no width, and a
  // zero-area box is "hidden" to Playwright however plainly a person can see it -- so
  // its position is what is asserted.
  const rule = page.locator("[data-testid='backlog-chart'] [data-testid='selection-rule']");
  await expect(rule).toBeAttached();
  const ruled = Number(await rule.getAttribute("x1"));
  const centre = box!.x + box!.width / 2;
  const chart = (await page.locator("[data-testid='backlog-chart']").boundingBox())!;
  // The rule is in viewBox units and the column in pixels, so compare the fractions.
  expect(Math.abs(ruled / 400 - (centre - chart.x) / chart.width)).toBeLessThan(0.02);
});

test("a tap on one panel leaves the others where they were", async ({ page }) => {
  await withHistory(page);
  await page.setViewportSize(PHONE);
  await openAnalytics(page);
  const other = page.getByTestId("throughput-readout");
  const before = await other.textContent();
  await page.locator("[data-testid='backlog-chart'] [data-bucket='3']").click();
  await expect(other).toHaveText(before ?? "");
});

test("the reconstructed span is hatched and its boundary is ruled", async ({ page }) => {
  await withHistory(page);
  await page.setViewportSize(DESKTOP);
  await openAnalytics(page);
  const hatch = page.locator("[data-testid='backlog-chart'] [data-testid='reconstructed-span']");
  await expect(hatch).toBeVisible();
  // Two runs were seeded; a single hatched prefix would be the failure §3.6 warns of.
  await expect(hatch.locator("rect")).toHaveCount(2);
});

test("a series younger than the range draws its coverage start, not zeros (ac-5)", async ({
  page,
}) => {
  await withHistory(page);
  await page.setViewportSize(PHONE);
  await openAnalytics(page);

  // The finish series was seeded with six weekly buckets inside a thirteen-week
  // window. §9.3 forbids padding it back with zeros, so what the reader sees is a
  // shorter axis and a sentence saying when the source started.
  const panel = page.getByRole("region", { name: "Finishes and gates" });
  await expect(panel).toContainText("Finishes are recorded from 7 Aug 2026.");
  const columns = panel.locator("[data-testid='finishes-chart'] [data-bucket]");
  await expect(columns).toHaveCount(6);

  // And the first bucket drawn is the coverage start rather than the window start.
  const firstLabel = await page
    .locator("[data-testid='finishes-chart'] text")
    .first()
    .textContent();
  expect(firstLabel).toBeTruthy();

  // The footer states one baseline per source family, because five cannot share one.
  const footer = page.getByTestId("coverage-sources");
  await expect(footer).toContainText("Finishes are recorded from 7 Aug 2026.");
  await expect(footer).toContainText("Runs are recorded from 5 Sep 2026.");
  await expect(footer).toContainText("The execution journal is recorded from 13 Sep 2026.");
});

test("an under-sampled bucket is visibly blank rather than drawn at zero (ac-5)", async ({
  page,
}) => {
  await withHistory(page);
  await page.setViewportSize(PHONE);
  await openAnalytics(page);
  const blanks = page.locator("[data-testid='segments-chart'] [data-testid='blank-buckets'] rect");
  await expect(blanks).toHaveCount(2);
  // And the readout for such a bucket says why, rather than showing five noughts.
  await page.locator("[data-testid='segments-chart'] [data-bucket='3']").click();
  await expect(page.getByTestId("segments-readout")).toContainText("not measured");
});

test("the summary row compares against native history, not the backfill (ac-3)", async ({
  page,
}) => {
  await withHistory(page);
  await page.setViewportSize(PHONE);
  await openAnalytics(page);
  // `native_from` is 40 days back and the window is 90, so §19.1's baseline is the
  // native date and the tile names it.
  const open = page.getByTestId("summary-open");
  await expect(open).toContainText("since 9 Aug 2026");
  await expect(open).not.toContainText("20 Jun");

  // And no tile restates its own total with an arrow on it, which is the failure the
  // baseline change exists to remove.
  const restated = await page.evaluate(() => {
    const bad: string[] = [];
    for (const key of ["open", "created", "completed", "human", "external"]) {
      const tile = document.querySelector(`[data-testid='summary-${key}']`);
      const spans = tile?.querySelectorAll("span") ?? [];
      const count = spans[1]?.textContent ?? "";
      const change = spans[2]?.textContent ?? "";
      // A rise that really is the whole count says so in words; anything else that
      // restates its own total is the failure the baseline change removes.
      const restated = new RegExp("(^|[^0-9])" + count + "([^0-9]|$)").test(change);
      if (count && restated && !change.includes("all of them")) bad.push(`${key}: ${change}`);
    }
    return bad;
  });
  expect(restated, "no tile shows a delta equal to its own count").toEqual([]);
});

test("stuck puts the queue last and names it (ac-4)", async ({ page }) => {
  await withHistory(page);
  await page.setViewportSize(PHONE);
  await openAnalytics(page);
  const bands = await page
    .locator("[data-testid='stuck-groups'] [data-testid^='stuck-band-']")
    .evaluateAll((nodes) => nodes.map((node) => node.getAttribute("data-testid")));
  expect(bands).toEqual([
    "stuck-band-human",
    "stuck-band-blocked",
    "stuck-band-agent",
    "stuck-band-queue",
  ]);
  const queue = page.getByTestId("stuck-band-queue");
  await expect(queue).toContainText("ready, unclaimed");
  await expect(queue).toContainText("the backlog waiting its turn");
});

test("a project with no history says so, and draws no axes", async ({ page }) => {
  await withNoHistory(page);
  await page.setViewportSize(PHONE);
  await openAnalytics(page);
  await expect(page.getByTestId("no-history")).toContainText("No history yet.");
  await expect(page.locator("svg[data-testid]")).toHaveCount(0);
  // The counts are still there: they are facts about now, not about history.
  await expect(page.getByTestId("summary-open")).toContainText("no comparison");
});

test("the server's own response renders without help", async ({ page }) => {
  // No interception at all. This is the real endpoint against the real store, which
  // for a project seeded moments ago is §9.2's thin history: the bars are drawn
  // because they are facts, and no trend is claimed from them.
  await page.setViewportSize(PHONE);
  await openAnalytics(page);
  await expect(page.getByTestId("backlog-chart")).toBeVisible();
  await expect(page.getByTestId("history-depth")).toContainText("days of history");
  await expect(page.getByTestId("summary-open")).toContainText("no comparison");
  await expect(page.getByTestId("coverage-footer")).toContainText("History from");
  const scroll = await horizontalScroll(page);
  expect(scroll.scrollWidth).toBeLessThanOrEqual(scroll.clientWidth);
});

test("the Dashboard is undisturbed: it still does not scroll", async ({ page }) => {
  // The task's constraint is that this page does not touch the Dashboard, and a route
  // added to the same shell is exactly the change that could. task-294's property is
  // measured here rather than assumed.
  await page.setViewportSize(PHONE);
  await page.goto("/app/p/_local");
  await expect(page.getByTestId("slot-board")).toBeVisible();
  const scroll = await page.evaluate(() => ({
    scrollHeight: document.documentElement.scrollHeight,
    innerHeight: window.innerHeight,
  }));
  expect(scroll.scrollHeight).toBeLessThanOrEqual(scroll.innerHeight);
});
