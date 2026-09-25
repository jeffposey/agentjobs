import { expect, test, type Page } from "@playwright/test";
import { writeFileSync } from "node:fs";

/**
 * How long it takes to open a task, measured the way a person experiences it.
 *
 * Click to *rendered*, not click to response. A fast endpoint sitting behind a
 * component that paints nothing until every field has arrived still feels slow, and
 * only a rendered-timing measurement notices the difference. The stop condition is
 * therefore the specification region being visible -- the same signal the functional
 * e2e specs use to decide a task page has actually opened.
 *
 * Two numbers, because they answer different questions:
 *
 * - **warm app** is the reported complaint: the app is already open, a row is
 *   clicked, and nothing appears for several seconds. This is the headline figure.
 * - **cold load** is the first visit, which pays for the bundle and the initial
 *   list fetch as well.
 *
 * **The warm figure is also taken inside the page, and that one is the headline**
 * (task-483). Timed from Node, the clock includes Playwright's own work: resolving a
 * `getByRole` locator and polling `toBeVisible()` both build an accessibility tree of
 * the page, and over a 580-row list a CPU profile put about 400 ms of a 636 ms click
 * in Playwright's `generateAriaTree` rather than in the app -- and that work runs on the
 * page's main thread, so it delays the app's own rendering too. The in-page figure runs
 * from the click event reaching the document to the first frame after the detail
 * region exists, with nothing polling the page meanwhile, which is the part a person
 * waits for. The Node-side figure is kept, under its old name, so reports stay
 * comparable with earlier runs; it now measures a page nobody polled until it painted.
 *
 * Driven by scripts/bench.py, which supplies the server, the corpus and the
 * iteration count through the environment and reads the timings back out of
 * BENCH_OUTPUT.
 */

const OUTPUT = process.env.BENCH_OUTPUT;
const ITERATIONS = Number(process.env.BENCH_ITERATIONS ?? "5");
const LIST_URL = "/app/p/_local/tasks?status=all";

type Measurement = {
  name: string;
  samples: number[];
  detail?: Record<string, unknown>;
};

const measurements: Measurement[] = [];

async function taskLinks(page: Page) {
  const region = page.getByRole("region", { name: "Tasks" });
  await expect(region).toBeVisible();
  const links = region.getByRole("link");
  await expect(links.first()).toBeVisible();
  return links;
}

type BenchWindow = { __openTask: Promise<number> };

/**
 * Start a clock in the page that the next click stops when the detail has painted.
 *
 * It starts on the click event reaching the document, so none of Playwright's locator
 * or actionability work is inside it, and stops on the frame after a detail region that
 * was not there at the click appears -- a region left over from an earlier task would
 * otherwise stop it at once.
 */
async function armInPageTimer(page: Page) {
  await page.evaluate(() => {
    const selector = 'section[aria-label="Full specification"]';
    (window as unknown as BenchWindow).__openTask = new Promise<number>((resolve) => {
      document.addEventListener(
        "click",
        () => {
          const clicked = performance.now();
          const previous = document.querySelector(selector);
          const look = () => {
            const region = document.querySelector(selector);
            if (region !== null && region !== previous) {
              requestAnimationFrame(() => setTimeout(() => resolve(performance.now() - clicked), 0));
            } else {
              requestAnimationFrame(look);
            }
          };
          look();
        },
        { capture: true, once: true },
      );
    });
  });
}

test("times opening a task, warm app and cold load", async ({ page }) => {
  const warm: number[] = [];
  const warmInPage: number[] = [];
  const cold: number[] = [];

  // Warm app: one page load, then click a different task each iteration. Different
  // tasks on purpose -- clicking the same row repeatedly would measure a cache hit
  // and report a number no user ever waits for.
  await page.goto(LIST_URL);
  const links = await taskLinks(page);
  const available = await links.count();
  expect(available).toBeGreaterThan(0);

  for (let index = 0; index < ITERATIONS; index += 1) {
    const rows = await taskLinks(page);
    const target = rows.nth(index % available);
    await target.scrollIntoViewIfNeeded();

    await armInPageTimer(page);
    const started = Date.now();
    await target.click();
    // The in-page clock is awaited before anything polls the page: `toBeVisible()`
    // walks the accessibility tree on the page's own main thread, so polling while the
    // app renders slows the app, not just the measurement of it.
    warmInPage.push(await page.evaluate(() => (window as unknown as BenchWindow).__openTask));
    await expect(page.getByRole("region", { name: "Full specification" })).toBeVisible();
    warm.push(Date.now() - started);

    await page.goBack();
  }

  // Cold load: a fresh navigation to the list, then a click. Includes whatever the
  // first paint of the list itself costs.
  for (let index = 0; index < ITERATIONS; index += 1) {
    const started = Date.now();
    await page.goto(LIST_URL);
    const rows = await taskLinks(page);
    await rows.nth(index % available).click();
    await expect(page.getByRole("region", { name: "Full specification" })).toBeVisible();
    cold.push(Date.now() - started);
  }

  measurements.push({
    name: "click event -> detail painted (warm app, in page)",
    samples: warmInPage.map((value) => Math.round(value)),
    detail: { rows_available: available },
  });
  measurements.push({
    name: "click task row -> detail rendered (warm app)",
    samples: warm,
    detail: { rows_available: available },
  });
  measurements.push({
    name: "cold load -> task detail rendered",
    samples: cold,
    detail: { rows_available: available },
  });
});

test.afterAll(() => {
  if (!OUTPUT) {
    throw new Error("BENCH_OUTPUT is required; run this config through scripts/bench.py.");
  }
  writeFileSync(OUTPUT, JSON.stringify({ measurements }, null, 2), "utf-8");
});
