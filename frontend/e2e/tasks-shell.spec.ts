import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

/**
 * task-237: the Tasks surface is two regions that scroll independently, and the page
 * under them does not scroll at all.
 *
 * Every claim here is a measured rectangle or a scroll offset read from Chromium,
 * because none of them exists in jsdom: it has no layout, so `overflow-y` is a string
 * on a style object and `scrollTop` never moves. The route table and what each viewport
 * renders are asserted in `src/App.shell.test.tsx`, which is where they belong; this
 * file is only for the half a browser has to answer. ENGINEERING.md, Verification.
 */

/** A landscape window, comfortably above the 600x600 device-class threshold. */
const LANDSCAPE = { width: 1280, height: 800 };
/** iPhone 14/15 CSS pixels, which is where this app is read over Tailscale. */
const PHONE = { width: 390, height: 844 };

/**
 * One record long enough that the detail region has somewhere to scroll, created once
 * and closed immediately.
 *
 * Closed, and that is the point rather than tidiness: every spec in this directory
 * shares one server and one project, and `queue-order.spec.ts` drags between two rows
 * that have to sit in one scrollport together, so an open fixture is a later spec's
 * failure.
 */
let longTaskId: string | null = null;

async function longTask(request: APIRequestContext) {
  if (longTaskId) return longTaskId;
  const paragraph =
    "A paragraph of the working specification, repeated until the rendered record is " +
    "taller than the region it is being read in. ";
  const created = await request.post("/api/tasks", {
    data: {
      title: "A record long enough to scroll inside its own region",
      summary: "Exists so the detail region has something to scroll.",
      description: paragraph.repeat(120),
      lifecycle: "ready",
      category: "ux",
      actor: "E2E Human",
    },
  });
  expect(created.ok()).toBeTruthy();
  longTaskId = (await created.json()).id as string;
  const closed = await request.post(`/api/tasks/${longTaskId}/close`, {
    data: { actor: "E2E Human", outcome: "completed", body: "Fixture; nothing to review." },
  });
  expect(closed.ok()).toBeTruthy();
  return longTaskId;
}

/**
 * The open record's own heading.
 *
 * By name rather than by level: the pinned bar carries an `<h1>` of its own, and on the
 * two-region shell a loading list and a loading record each add one, so `level: 1` is
 * three elements and a strict-mode violation.
 */
function recordTitle(page: Page) {
  return page.getByRole("heading", { name: /A record long enough to scroll/ });
}

/** A region's box and how far it can scroll, read from the browser. */
async function region(page: Page, name: "list" | "detail") {
  return page.evaluate((which) => {
    const element = document.querySelector(`[data-region="${which}"]`);
    if (!element) return null;
    const box = element.getBoundingClientRect();
    return {
      x: box.x,
      right: box.right,
      top: box.top,
      bottom: box.bottom,
      room: element.scrollHeight - element.clientHeight,
      scrollTop: element.scrollTop,
    };
  }, name);
}

test("the list and the record sit side by side, and the page itself does not scroll", async ({
  page,
  request,
}) => {
  const taskId = await longTask(request);
  await page.setViewportSize(LANDSCAPE);
  await page.goto(`/app/p/_local/tasks/${taskId}?status=all`);
  await expect(page.getByRole("region", { name: "Tasks" })).toBeVisible();
  await expect(recordTitle(page)).toBeVisible();

  const list = await region(page, "list");
  const detail = await region(page, "detail");
  expect(list, "the list region did not render").not.toBeNull();
  expect(detail, "the detail region did not render").not.toBeNull();

  // Side by side rather than one above the other, asked of the geometry rather than of
  // a class name: a `grid-cols` typo that stacked them would keep every class intact.
  expect(list!.right).toBeLessThanOrEqual(detail!.x + 1);
  expect(list!.top).toBeLessThan(detail!.bottom);

  // Both fit the window, and the window is what the whole surface is: the page has
  // nowhere to scroll, which is what stops reading a record carrying the list away.
  const page_ = await page.evaluate(() => ({
    room: document.documentElement.scrollHeight - window.innerHeight,
    sideways: document.documentElement.scrollWidth - document.documentElement.clientWidth,
  }));
  expect(page_.room, "the Tasks surface grew the page instead of filling the window").toBeLessThanOrEqual(1);
  expect(page_.sideways, "the Tasks surface pushed the page sideways").toBe(0);
});

test("scrolling the record leaves the list where it was", async ({ page, request }) => {
  const taskId = await longTask(request);
  await page.setViewportSize(LANDSCAPE);
  await page.goto(`/app/p/_local/tasks/${taskId}?status=all`);
  await expect(recordTitle(page)).toBeVisible();

  const before = await region(page, "detail");
  expect(before!.room, "the record is not long enough to scroll").toBeGreaterThan(200);

  await page.evaluate(() => {
    const detail = document.querySelector('[data-region="detail"]')!;
    detail.scrollTop = detail.scrollHeight;
  });

  const detail = await region(page, "detail");
  const list = await region(page, "list");
  expect(detail!.scrollTop, "the record did not scroll inside its own region").toBeGreaterThan(100);
  expect(list!.scrollTop, "reading the record moved the list").toBe(0);
  // The list is still exactly where it was: not merely unscrolled, but on screen.
  expect(list!.top).toBeGreaterThanOrEqual(0);
  expect(list!.bottom).toBeLessThanOrEqual(LANDSCAPE.height + 1);
});

test("selecting another task changes the record and leaves the list scrolled where it was", async ({
  page,
  request,
}) => {
  await longTask(request);
  await page.setViewportSize(LANDSCAPE);
  await page.goto("/app/p/_local/tasks");
  await expect(page.getByRole("region", { name: "Tasks" })).toBeVisible();

  const room = (await region(page, "list"))!.room;
  // A list shorter than its region has nothing to lose, so there would be nothing to
  // assert. The shared corpus is normally far longer than one screen; skip rather than
  // pass silently if it is not.
  test.skip(room < 100, "this project's list fits its region, so there is no scroll to keep");

  await page.evaluate(() => {
    const list = document.querySelector('[data-region="list"]')!;
    list.scrollTop = 200;
  });
  const row = page.locator("[data-task] a[href*='/tasks/']").first();
  const href = await row.getAttribute("href");
  await row.click();
  await expect.poll(() => new URL(page.url()).pathname).toBe(href);
  // Whichever task the first row happens to be: the claim is about the list, so the
  // record only has to have arrived in its own region.
  await expect(page.locator('[data-region="detail"] h1')).toBeVisible();

  // The defect this epic exists to remove: opening a task used to unmount the list and
  // throw its scroll position away.
  expect((await region(page, "list"))!.scrollTop).toBe(200);
});

test("a phone still gets one thing at a time, and a page that scrolls", async ({
  page,
  request,
}) => {
  const taskId = await longTask(request);
  await page.setViewportSize(PHONE);
  await page.goto(`/app/p/_local/tasks/${taskId}?status=all`);
  await expect(recordTitle(page)).toBeVisible();

  expect(await region(page, "list"), "a phone was given the list region").toBeNull();
  expect(await region(page, "detail"), "a phone was given the detail region").toBeNull();
  expect(
    await page.evaluate(() => document.documentElement.scrollHeight - window.innerHeight),
    "the phone's page stopped scrolling",
  ).toBeGreaterThan(200);
});
