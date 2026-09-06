import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

/**
 * task-356: the filter controls behind a button rather than permanently on screen.
 *
 * Everything here is a claim jsdom cannot settle -- a real button's own keyboard
 * activation, a measured box against a measured viewport, an element that has to be
 * *inside* the window rather than merely in the document.
 * `src/components/TaskList.test.tsx` covers the wiring, the URL round trip and the
 * indicator's arithmetic, which is where those belong. ENGINEERING.md, Verification.
 *
 * **The keyboard here is Chromium's own default action on a `<button>`**, which
 * `Input.dispatchKeyEvent` does drive -- unlike an application shortcut, which
 * Playwright's keyboard reaches below (task-225). That is why the jsdom suite asserts
 * the element is a real button and leaves the keys to this file.
 */

/**
 * Exactly the floor of the list column, as `task-tree.spec.ts` measures it:
 * `minmax(20rem, min(34%, 36rem))` is 320px for any window where 34% is less than
 * that. Tall enough to clear the 600px device-class threshold.
 */
const NARROWEST_TWO_REGION = { width: 700, height: 800 };
/** Narrow and short enough that the device-class rule gives it the stacked shell. */
const PHONE = { width: 390, height: 700 };

/** Narrows the shared corpus to this spec's own rows. */
const TOKEN = "gh356filter";

/**
 * Enough rows that the list is taller than any window this spec opens.
 *
 * The point of the seed is the *first* one: with the filter panel gone, row one has to
 * be on screen without a scroll, and a list of one row would pass that whatever the
 * header above it cost.
 */
async function seed(request: APIRequestContext) {
  const created: Array<string> = [];
  for (let index = 0; index < 20; index += 1) {
    const response = await request.post("/api/tasks", {
      data: {
        title: `${TOKEN} row ${String(index).padStart(2, "0")}`,
        summary: "task-356 fixture.",
        description: "Exists so the list under the filter button is longer than a window.",
        lifecycle: "ready",
        category: "ux",
        priority: "low",
        actor: "E2E Human",
      },
    });
    expect(response.ok()).toBeTruthy();
    created.push((await response.json()).id as string);
  }
  return {
    first: created[0],
    release: async () => {
      for (const id of [...created].reverse()) {
        const closed = await request.post(`/api/tasks/${id}/close`, {
          data: { actor: "E2E Human", outcome: "cancelled", body: "Filter fixture." },
        });
        expect(closed.ok()).toBeTruthy();
      }
    },
  };
}

/**
 * What the list may spend on its own header before the first row, in the 320px column.
 *
 * **Measured, not chosen.** This spec was run against both arms at 700x800 on
 * 2026-09-06: `main`'s permanent panel cost **358px**, and the button costs **135px**.
 * The budget sits between them and near the new figure -- deliberately not *at* it,
 * because a budget that fails on a one-pixel font change is a budget somebody deletes,
 * and far enough below 358 that putting the panel back cannot pass.
 */
const HEADER_BUDGET_PX = 180;

/** How far below the top of the list region the first row starts. */
async function headerCost(page: Page, taskId: string) {
  return page.evaluate((id) => {
    const region = document.querySelector('[data-region="list"]');
    const row = document.querySelector(`[data-task="${id}"]`);
    if (!region || !row) throw new Error("No list region, or no first row in it.");
    return Math.round(row.getBoundingClientRect().top - region.getBoundingClientRect().top);
  }, taskId);
}

function filterButton(page: Page) {
  return page.getByRole("button", { name: /^Filters/ });
}

function popover(page: Page) {
  return page.getByRole("dialog", { name: "Filters" });
}

test.describe("the filter controls behind a button", () => {
  let fixtures: Awaited<ReturnType<typeof seed>>;
  let api: APIRequestContext;

  test.beforeAll(async ({ playwright, baseURL }) => {
    api = await playwright.request.newContext({ baseURL });
    fixtures = await seed(api);
  });

  test.afterAll(async () => {
    await fixtures.release();
    await api.dispose();
  });

  test("the first task row is on screen without a scroll, in the narrowest sidebar", async ({
    page,
  }) => {
    await page.setViewportSize(NARROWEST_TWO_REGION);
    await page.goto(`/app/p/_local/tasks?q=${TOKEN}`);
    const row = page.locator(`[data-task="${fixtures.first}"]`);
    await expect(row).toBeVisible();

    // The column really is at its floor, so this is a claim about the narrowest case
    // rather than about whatever width this window happened to give the list.
    const column = await page.evaluate(() => {
      const region = document.querySelector('[data-region="list"]');
      return region ? Math.round(region.getBoundingClientRect().width) : null;
    });
    expect(column).toBeLessThanOrEqual(321);

    // Nothing has been scrolled to get there -- neither the page nor the list region.
    const scrolled = await page.evaluate(() => ({
      page: window.scrollY,
      region: document.querySelector('[data-region="list"]')?.scrollTop ?? -1,
    }));
    expect(scrolled).toEqual({ page: 0, region: 0 });

    // And the whole row is inside the window, not merely its first pixel.
    const box = await row.boundingBox();
    expect(box, "the first row has no box at all").not.toBeNull();
    expect(box!.y).toBeGreaterThanOrEqual(0);
    expect(box!.y + box!.height).toBeLessThanOrEqual(NARROWEST_TWO_REGION.height);

    // The assertion that would have caught the thing this task fixes.
    //
    // "Row one is visible in an 800px window" passes with the old panel in place too --
    // it was a decoration. What actually changed is measurable: how much of the column
    // the list spends before its first row. `console.log` here rather than a bare
    // number, so the next person reading a failure gets the measurement rather than
    // only the verdict.
    const spent = await headerCost(page, fixtures.first);
    console.log(`[task-356] the list spends ${spent}px above its first row at 320px`);
    expect(spent).toBeLessThanOrEqual(HEADER_BUDGET_PX);

    // The three selects are not merely off screen; they are not rendered.
    await expect(page.getByRole("combobox", { name: "Status" })).toHaveCount(0);
    await expect(filterButton(page)).toHaveAttribute("aria-expanded", "false");
  });

  test("Enter opens it, Escape closes it, and focus lands back on the button", async ({ page }) => {
    await page.setViewportSize(NARROWEST_TWO_REGION);
    await page.goto(`/app/p/_local/tasks?q=${TOKEN}`);

    await filterButton(page).focus();
    await page.keyboard.press("Enter");
    await expect(popover(page)).toBeVisible();
    await expect(filterButton(page)).toHaveAttribute("aria-expanded", "true");
    // Opening put focus somewhere usable rather than leaving it behind the button.
    await expect(page.getByRole("combobox", { name: "Status" })).toBeFocused();

    await page.keyboard.press("Escape");
    await expect(popover(page)).toHaveCount(0);
    await expect(filterButton(page)).toBeFocused();

    // Space, which a browser activates on keyup rather than keydown -- a different code
    // path in Chromium from Enter, and the one a hand-rolled control usually misses.
    await page.keyboard.press("Space");
    await expect(popover(page)).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(popover(page)).toHaveCount(0);
  });

  test("a click outside closes it and the click reaches what it was aimed at", async ({ page }) => {
    await page.setViewportSize(NARROWEST_TWO_REGION);
    await page.goto(`/app/p/_local/tasks/${fixtures.first}?q=${TOKEN}`);
    await filterButton(page).click();
    await expect(popover(page)).toBeVisible();

    // Something with a job of its own, not empty space: the dismissal must not swallow
    // the gesture, which is what a `mousedown` handler calling `preventDefault` would do.
    await page.getByRole("searchbox", { name: "Search tasks" }).click();
    await expect(popover(page)).toHaveCount(0);
    await expect(page.getByRole("searchbox", { name: "Search tasks" })).toBeFocused();
  });

  test("opens inside the window on a phone, below the pinned header", async ({ page }) => {
    await page.setViewportSize(PHONE);
    await page.goto(`/app/p/_local/tasks?q=${TOKEN}`);
    await filterButton(page).click();

    const box = await popover(page).boundingBox();
    expect(box, "the popover has no box at all").not.toBeNull();
    expect(box!.x).toBeGreaterThanOrEqual(0);
    expect(box!.x + box!.width).toBeLessThanOrEqual(PHONE.width);
    expect(box!.y).toBeGreaterThanOrEqual(0);
    expect(box!.y + box!.height).toBeLessThanOrEqual(PHONE.height);

    // It opened *below* the header rather than under it. Asked of the browser's own hit
    // testing: whatever is painted at the popover's top-left has to be the popover.
    const onTop = await page.evaluate(() => {
      const dialog = document.getElementById("task-filter-popover");
      if (!dialog) return "no popover";
      const rect = dialog.getBoundingClientRect();
      const hit = document.elementFromPoint(rect.x + 4, rect.y + 4);
      return dialog.contains(hit) ? "the popover" : (hit?.tagName ?? "nothing");
    });
    expect(onTop).toBe("the popover");

    // The page did not grow sideways to hold it.
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
      ),
    ).toBe(0);
  });

  test("a filtered URL arrives filtered, and one gesture clears it", async ({ page }) => {
    await page.setViewportSize(NARROWEST_TWO_REGION);
    await page.goto(`/app/p/_local/tasks?q=${TOKEN}&priority=critical`);

    // Nothing matches, and the reason is legible without opening anything: this is the
    // "empty backlog that is really a filtered one" case the spec calls the worse bug.
    await expect(page.getByText("No tasks match these filters.")).toBeVisible();
    await expect(page.getByTestId("active-filter-count")).toHaveText("1");

    await filterButton(page).click();
    await page.getByRole("button", { name: "Clear all filters" }).click();

    await expect(page.getByTestId("active-filter-count")).toHaveCount(0);
    await expect(page.getByRole("searchbox", { name: "Search tasks" })).toHaveValue("");
    expect(new URL(page.url()).search).toBe("");
  });
});
