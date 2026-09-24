import { expect, test, type Page } from "./fixtures";

/**
 * task-368: the divider between the task list and the record, measured in Chromium.
 *
 * The arithmetic and the aria values are `src/components/ListDetailSplit.test.tsx`;
 * this file is the half only a browser can answer -- whether the regions actually take
 * the widths the track asks for, whether a drag moves them, and whether a reload keeps
 * the result.
 *
 * The instrument is stated rather than assumed: `page.mouse` goes in through CDP's
 * `Input.dispatchMouseEvent`, which the browser turns into real pointer events with
 * hit-testing and capture. Good evidence the handlers, the clamp and the layout work;
 * no evidence about a finger on a tablet, which is ac-2 and a person's job.
 */

const DESKTOP = { width: 1280, height: 800 };
const TABLET = { width: 1024, height: 768 };
const PHONE = { width: 390, height: 844 };
const KEY = "agentjobs.tasks.listWidth";

async function widths(page: Page) {
  return page.evaluate(() => {
    const width = (css: string) =>
      Math.round(document.querySelector(css)?.getBoundingClientRect().width ?? -1);
    return {
      list: width('[data-region="list"]'),
      detail: width('[data-region="detail"]'),
      sideways: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    };
  });
}

async function open(page: Page) {
  await page.goto("/app/p/_local/tasks");
  await expect(page.getByRole("region", { name: "Tasks" })).toBeVisible();
  return page.getByRole("separator", { name: "Resize the task list" });
}

/** Drag the divider by `dx` with the mouse, in steps so moves are delivered between. */
async function drag(page: Page, dx: number) {
  const box = (await page.getByRole("separator", { name: "Resize the task list" }).boundingBox())!;
  const x = box.x + box.width / 2;
  const y = box.y + box.height / 2;
  await page.mouse.move(x, y);
  await page.mouse.down();
  await page.mouse.move(x + dx / 2, y, { steps: 4 });
  const midway = await widths(page);
  await page.mouse.move(x + dx, y, { steps: 4 });
  await page.mouse.up();
  return midway;
}

test.beforeEach(async ({ page }) => {
  await page.goto("/app/p/_local/tasks");
  await page.evaluate((key) => window.localStorage.removeItem(key), KEY);
});

test("a mouse drag moves the boundary both ways, live, and a reload keeps it", async ({ page }) => {
  await page.setViewportSize(DESKTOP);
  const separator = await open(page);
  const before = await widths(page);
  // Today's ratio, unchanged: 34% of a 1216px grid, and task-239's 779px record.
  expect(before.list).toBe(413);
  expect(before.detail).toBe(779);

  const midway = await drag(page, -60);
  expect(midway.list, "the list did not follow the pointer mid-drag").toBeLessThan(before.list);
  const narrower = await widths(page);
  expect(narrower.list).toBe(353);
  expect(narrower.detail).toBe(839);
  expect(await page.evaluate((key) => window.localStorage.getItem(key), KEY)).toBe("353");

  await drag(page, 40);
  expect((await widths(page)).list).toBe(393);

  await page.reload();
  await expect(separator).toHaveAttribute("aria-valuenow", "393");
  expect((await widths(page)).list).toBe(393);

  await page.evaluate((key) => window.localStorage.removeItem(key), KEY);
  await page.reload();
  await expect(separator).toHaveAttribute("aria-valuenow", "413");
  expect((await widths(page)).list).toBe(413);
});

test("neither region can be dragged under its floor", async ({ page }) => {
  await page.setViewportSize(DESKTOP);
  await open(page);
  await drag(page, -400);
  expect((await widths(page)).list, "the list went under 20rem").toBe(320);
  await drag(page, 800);
  const wide = await widths(page);
  expect(wide.detail, "the record was pushed under its 768px container threshold").toBe(768);
  expect(wide.sideways).toBeLessThanOrEqual(1);
});

test("a 1024 tablet cannot narrow the record past what the default gave it", async ({ page }) => {
  await page.setViewportSize(TABLET);
  const separator = await open(page);
  const before = await widths(page);
  await expect(separator).toHaveAttribute("aria-valuemax", String(before.list));
  await drag(page, 300);
  expect((await widths(page)).detail).toBe(before.detail);
  await drag(page, -300);
  expect((await widths(page)).list).toBe(320);
});

test("a stored width wider than this window is clamped, not lost", async ({ page }) => {
  await page.setViewportSize({ width: 1920, height: 1000 });
  await open(page);
  await drag(page, 400);
  const stored = await page.evaluate((key) => window.localStorage.getItem(key), KEY);
  expect(Number(stored)).toBe(976);
  // Polled: the grid is re-measured by a ResizeObserver, whose callback lands after the
  // resize's layout and before its paint -- so a person never sees the squeezed frame,
  // but a read straight after `setViewportSize` can.
  await page.setViewportSize(DESKTOP);
  await expect.poll(async () => (await widths(page)).detail).toBe(768);
  await page.setViewportSize({ width: 1920, height: 1000 });
  await expect.poll(async () => (await widths(page)).list).toBe(976);
  expect(await page.evaluate((key) => window.localStorage.getItem(key), KEY)).toBe("976");
});

test("a portrait phone has no divider", async ({ page }) => {
  await page.setViewportSize(PHONE);
  await page.goto("/app/p/_local/tasks");
  await expect(page.getByRole("region", { name: "Tasks" })).toBeVisible();
  await expect(page.getByRole("separator", { name: "Resize the task list" })).toHaveCount(0);
  expect((await widths(page)).sideways).toBeLessThanOrEqual(1);
});
