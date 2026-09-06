import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

/**
 * task-239: the record fits the region task-237 gave it, at both ends of the range.
 *
 * Every claim here is a rectangle read from Chromium. None of it exists in jsdom, which
 * has no layout: `max-w-[78ch]` there is a string on a class list, a container query
 * never evaluates, and `sticky` never sticks. The half a browser is not needed for --
 * that no control was lost when the panel was refitted -- is
 * `src/components/TaskDetail.actions.test.tsx`, which lists them. ENGINEERING.md,
 * Verification.
 *
 * The instrument matters and is stated rather than assumed: this file scrolls by
 * assigning `scrollTop` and resizes by `setViewportSize`, both of which are the browser
 * doing its own layout. Neither is a person's gesture and nothing here claims to be one.
 */

/** A monitor wide enough that an uncapped measure would be unreadable. */
const WIDE = { width: 2560, height: 1440 };
/** The narrowest landscape window the device-class threshold admits as "not a phone". */
const NARROW = { width: 900, height: 700 };

/**
 * One record with prose in every field and a log long enough to scroll, created once
 * and closed immediately.
 *
 * Closed for the reason `tasks-shell.spec.ts` gives: these specs share one server and
 * one project, and an open fixture is a later spec's failure.
 */
let fitTaskId: string | null = null;

async function fitTask(request: APIRequestContext) {
  if (fitTaskId) return fitTaskId;
  const paragraph =
    "A paragraph of the working specification, long enough that its rendered width is " +
    "the thing being measured rather than an accident of how much text there is. ";
  const created = await request.post("/api/tasks", {
    data: {
      title: "A record wide enough to measure the panel with",
      summary: paragraph,
      description: paragraph.repeat(40),
      constraints: paragraph.repeat(4),
      lifecycle: "ready",
      category: "ux",
      actor: "E2E Human",
    },
  });
  expect(created.ok()).toBeTruthy();
  fitTaskId = (await created.json()).id as string;
  for (let n = 0; n < 12; n += 1) {
    // One entry carries a token nothing can break, which is the input most likely to
    // push a panel sideways rather than wrap inside it.
    const body = n === 5 ? `unbroken-${"x".repeat(400)}` : `${paragraph}Pass ${n}.`;
    const logged = await request.post(`/api/tasks/${fitTaskId}/log`, {
      data: { actor: "E2E Human", type: "progress", body },
    });
    expect(logged.ok()).toBeTruthy();
  }
  const closed = await request.post(`/api/tasks/${fitTaskId}/close`, {
    data: { actor: "E2E Human", outcome: "completed", body: "Fixture; nothing to review." },
  });
  expect(closed.ok()).toBeTruthy();
  return fitTaskId;
}

/** The rendered width of one element, or null when it is not on the page. */
async function widthOf(page: Page, selector: string) {
  return page.evaluate((css) => {
    const element = document.querySelector(css);
    return element ? Math.round(element.getBoundingClientRect().width) : null;
  }, selector);
}

test("prose holds a measure while the wide blocks take the width", async ({ page, request }) => {
  const taskId = await fitTask(request);
  await page.setViewportSize(WIDE);
  await page.goto(`/app/p/_local/tasks/${taskId}?status=all`);
  // The record itself, not `h1`: the "Opening task..." card carries one too, so waiting
  // on the heading passes while the region still holds the loading state -- and then
  // measures a panel that has no prose in it yet. Cost an hour, once.
  await expect(page.getByRole("region", { name: "Full specification" })).toBeVisible();

  const region = (await widthOf(page, '[data-region="detail"]'))!;
  // The premise. Without a wide region there is nothing for a measure to protect
  // against, and every assertion below would pass on a layout that had done nothing.
  expect(region, "the detail region is not wide enough for this test to mean anything").toBeGreaterThan(1200);

  const prose = await page.evaluate(() =>
    Array.from(
      document.querySelectorAll('[aria-label="Full specification"] .whitespace-pre-wrap'),
    ).map((element) => Math.round(element.getBoundingClientRect().width)),
  );
  expect(prose.length, "no prose was found to measure").toBeGreaterThan(0);
  for (const width of prose) {
    // 78ch of this panel's `text-sm`, which lands near 590px. The bound is generous
    // because the exact figure is a font metric; what is being asserted is that the
    // line length stopped being a function of the monitor.
    expect(width, "prose was allowed to run the full width of the region").toBeLessThan(800);
  }

  // The other half, and the half that a single cap around the whole record would fail:
  // blocks that are better wide are still wide.
  const metadata = (await widthOf(page, '[aria-label="Task metadata"]'))!;
  const log = (await widthOf(page, '[aria-label="Task log"]'))!;
  expect(metadata, "the metadata strip was capped along with the prose").toBeGreaterThan(region - 20);
  expect(log, "the log was capped along with the prose").toBeGreaterThan(region - 20);

  // And the prose-and-controls cards sit between the two, at the block measure.
  const spec = (await widthOf(page, '[aria-label="Full specification"]'))!;
  expect(spec).toBeGreaterThan(700);
  expect(spec).toBeLessThan(1000);
});

test("nothing is clipped and nothing scrolls sideways at the narrowest landscape window", async ({
  page,
  request,
}) => {
  const taskId = await fitTask(request);
  await page.setViewportSize(NARROW);
  await page.goto(`/app/p/_local/tasks/${taskId}?status=all`);
  await expect(page.getByRole("region", { name: "Full specification" })).toBeVisible();

  // Every long entry open, because the unbreakable token is inside a collapsed
  // `<details>` by default and a horizontal-overflow test that never renders the
  // overflowing content is not a test.
  await page.getByRole("button", { name: "Expand all entries" }).click();

  const overflow = await page.evaluate(() => {
    const detail = document.querySelector('[data-region="detail"]')!;
    const list = document.querySelector('[data-region="list"]')!;
    return {
      detail: detail.scrollWidth - detail.clientWidth,
      list: list.scrollWidth - list.clientWidth,
      page: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    };
  });
  expect(overflow.detail, "the record scrolls sideways inside its own region").toBeLessThanOrEqual(1);
  expect(overflow.list, "the list scrolls sideways inside its own region").toBeLessThanOrEqual(1);
  expect(overflow.page, "the page scrolls sideways").toBeLessThanOrEqual(1);

  // The panel restacked because the *panel* is narrow, not because the window is: at
  // 900px the window is well past the 768px threshold and the region is nowhere near
  // it. Asserted through the rendered type size, which is what a reader would notice.
  const titleSize = await page.evaluate(() => {
    const h1 = document.querySelector('[data-region="detail"] h1')!;
    return parseFloat(getComputedStyle(h1).fontSize);
  });
  expect(titleSize, "the title is still sized for a window it does not have").toBeLessThan(28);
});

test("the record's id, title and status stay put while its log scrolls", async ({
  page,
  request,
}) => {
  const taskId = await fitTask(request);
  await page.setViewportSize(NARROW);
  await page.goto(`/app/p/_local/tasks/${taskId}?status=all`);
  await expect(page.getByRole("region", { name: "Full specification" })).toBeVisible();
  await page.getByRole("button", { name: "Expand all entries" }).click();

  const room = await page.evaluate(() => {
    const detail = document.querySelector('[data-region="detail"]')!;
    return detail.scrollHeight - detail.clientHeight;
  });
  expect(room, "the record is not long enough to scroll").toBeGreaterThan(400);

  await page.evaluate(() => {
    const detail = document.querySelector('[data-region="detail"]')!;
    detail.scrollTop = detail.scrollHeight;
  });

  const after = await page.evaluate(() => {
    const header = document.querySelector('[data-region="detail"] header[data-pinned="yes"]');
    const detail = document.querySelector('[data-region="detail"]')!;
    if (!header) return null;
    const box = header.getBoundingClientRect();
    const region = detail.getBoundingClientRect();
    return {
      scrolled: Math.round(detail.scrollTop),
      // Still inside the region it belongs to, at the top of it.
      top: Math.round(box.top - region.top),
      id: header.querySelector(".select-all")?.textContent ?? null,
      title: header.querySelector("h1")?.textContent ?? null,
      status: header.querySelector("span")?.textContent ?? null,
    };
  });
  expect(after, "the header is not marked pinned in the two-region shell").not.toBeNull();
  expect(after!.scrolled, "the record did not scroll").toBeGreaterThan(300);
  expect(after!.top, "the header scrolled away with the log").toBeLessThanOrEqual(1);
  expect(after!.id).toContain(taskId);
  expect(after!.title).toContain("A record wide enough");
  expect(after!.status, "the ball is no longer readable from the pinned header").toBeTruthy();
});

test("Dashboard, Create and Dispatch still fill the window and have no sidebar", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1600, height: 900 });
  for (const [name, path] of [
    ["Dashboard", ""],
    ["Create", "/tasks/new"],
    ["Dispatch", "/dispatch"],
  ] as const) {
    await page.goto(`/app/p/_local${path}`);
    await expect(page.getByRole("navigation").first()).toBeVisible();

    const shape = await page.evaluate(() => {
      const main = document.querySelector("main")!;
      return {
        list: Boolean(document.querySelector('[data-region="list"]')),
        detail: Boolean(document.querySelector('[data-region="detail"]')),
        main: Math.round(main.getBoundingClientRect().width),
        sideways: document.documentElement.scrollWidth - document.documentElement.clientWidth,
      };
    });
    expect(shape.list, `${name} was given the task list column`).toBe(false);
    expect(shape.detail, `${name} was given the detail region`).toBe(false);
    // `max-w-7xl` is 80rem; at 1600 the page is the full window less its gutters. The
    // claim is that the shell change did not narrow it, so the bound is loose and one
    // sided.
    expect(shape.main, `${name} was narrowed by the shell change`).toBeGreaterThan(1200);
    expect(shape.sideways, `${name} scrolls sideways`).toBeLessThanOrEqual(1);
  }
});

test("filing a task from the Tasks surface takes the whole width", async ({ page }) => {
  await page.setViewportSize({ width: 1600, height: 900 });
  await page.goto("/app/p/_local/tasks");
  await expect(page.getByRole("region", { name: "Tasks" })).toBeVisible();
  expect(await widthOf(page, '[data-region="list"]'), "the list region did not render").not.toBeNull();

  // The route the empty detail region offers, which is the path a person actually takes
  // from here. task-239's decision: Create leaves the two-region shell rather than
  // rendering into the residual column beside the list.
  await page.getByRole("link", { name: "File a new task" }).click();
  await expect(page.getByRole("heading", { name: /Give the next reader enough/ })).toBeVisible();
  expect(await widthOf(page, '[data-region="list"]'), "Create kept the list column").toBeNull();
  await expect(page.getByRole("navigation").first()).toBeVisible();
});
