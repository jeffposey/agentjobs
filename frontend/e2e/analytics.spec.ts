import { expect, test, type Page, type Route } from "@playwright/test";

/**
 * task-373: the analytics page, measured in a browser rather than asserted in jsdom.
 *
 * The split between this file and `src/components/Analytics.test.tsx` is the whole of
 * `docs/analytics-design.md` §10.1's argument. jsdom returns zeros from every
 * rectangle, has no `ResizeObserver` and no `getBBox`, so it can say what a chart
 * *claims* and nothing about what it *occupies*. Everything measured below is a real
 * rectangle in Chromium:
 *
 * - the page fits a 390px phone with no horizontal scroll (ac-6),
 * - a tap on a chart column updates that chart's readout (ac-6),
 * - the axis labels are large enough to read at that width,
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
        summary: "A fixture for task-373's browser measurements. Closed when this file finishes.",
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
 */
async function withHistory(page: Page) {
  await page.route("**/api/projects/*/analytics*", async (route: Route) => {
    const response = await route.fetch();
    const body = (await response.json()) as AnalyticsBody;
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
    body.throughput = Array.from({ length: 13 }, (_, index) => {
      const completed = index % 6;
      return {
        bucket: dayKey((12 - index) * 7),
        tasks_completed: completed,
        completion_events: completed + (index === 4 ? 1 : 0),
        cancelled: index % 5 === 0 ? 1 : 0,
        cycle_p50_days: completed >= 3 ? 2 + index * 0.3 : null,
        cycle_p90_days: completed >= 3 ? 6 + index * 0.5 : null,
        sample: completed,
      };
    });
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

test("the route survives a direct visit and a reload (ac-1)", async ({ page }) => {
  await withHistory(page);
  await page.setViewportSize(DESKTOP);
  await openAnalytics(page);
  await page.reload();
  await expect(page.getByTestId("analytics-page")).toBeVisible();
  await expect(page).toHaveURL(new RegExp(`${ANALYTICS}$`));
});

test("the six regions are in the order of the four questions (ac-1)", async ({ page }) => {
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
    "Throughput and cycle time",
    "Aging",
    "The ten oldest open tasks",
    "Stuck",
    "What this page can claim",
  ]);
});

test("nothing scrolls horizontally on a 390px phone (ac-6)", async ({ page }) => {
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

test("the axis labels are readable at 390px (ac-6)", async ({ page }) => {
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

test("tapping a chart column updates its readout (ac-6)", async ({ page }) => {
  await withHistory(page);
  await page.setViewportSize(PHONE);
  await openAnalytics(page);

  const readout = page.getByTestId("backlog-readout");
  const before = await readout.textContent();

  // A real click on a real element, and the proof that it landed is the readout
  // changing -- ENGINEERING.md's rule, because an automation tool reports success
  // whether or not any event arrived. The target is the column's full height.
  const column = page.locator("[data-testid='backlog-chart'] [data-bucket='20']");
  const box = await column.boundingBox();
  expect(box, "the hit target is a real rectangle in the browser").not.toBeNull();
  expect(box!.height, "the hit target is the full column height").toBeGreaterThan(60);
  await column.click();

  await expect(readout).not.toHaveText(before ?? "");
  await expect(column).toHaveAttribute("aria-pressed", "true");
  // The rule moves to the tapped column too, so the selection is marked on the chart
  // and not only stated in the line above. `toBeAttached` rather than `toBeVisible`:
  // a vertical line has no width, and a zero-area box is "hidden" to Playwright
  // however plainly a person can see it -- so its position is what is asserted.
  const rule = page.locator("[data-testid='backlog-chart'] [data-testid='selection-rule']");
  await expect(rule).toBeAttached();
  const ruled = Number(await rule.getAttribute("x1"));
  const centre = box!.x + box!.width / 2;
  const chart = (await page.locator("[data-testid='backlog-chart']").boundingBox())!;
  // The rule is in viewBox units and the column in pixels, so compare the fractions.
  expect(Math.abs(ruled / 400 - (centre - chart.x) / chart.width)).toBeLessThan(0.02);
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

test("a project with no history says so, and draws no axes (ac-5)", async ({ page }) => {
  await withNoHistory(page);
  await page.setViewportSize(PHONE);
  await openAnalytics(page);
  await expect(page.getByTestId("no-history")).toContainText("No history yet.");
  await expect(page.locator("svg[data-testid]")).toHaveCount(0);
  // The counts are still there: they are facts about now, not about history.
  await expect(page.getByTestId("summary-open")).toContainText("no comparison");
});

test("the server's own response renders without help (ac-1, ac-5)", async ({ page }) => {
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

test("the Dashboard is undisturbed: it still does not scroll (ac-7)", async ({ page }) => {
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
