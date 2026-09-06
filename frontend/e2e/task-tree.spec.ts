import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

/**
 * task-238: the task list as the master column beside the record.
 *
 * Everything here is a claim a browser has to settle and jsdom cannot -- a measured
 * column width, a real font size, a scroll offset that survived a click, a fold that
 * survived a reload because it went to `localStorage` rather than to React state.
 * `src/components/TaskList.test.tsx` covers the arithmetic and the wiring, which is
 * where those belong. ENGINEERING.md, Verification.
 *
 * **Not evidence for a gesture a hand makes.** Playwright drives Chromium's keyboard
 * below the browser's own shortcut handling and its drag through
 * `Input.setInterceptDrags`; task-225 is the afternoon lost to forgetting that. The
 * by-hand checks for Alt+Up twice in a row and for a real mouse drag are recorded on
 * task-238 itself, with the instrument named.
 */

/**
 * Exactly the floor of the list column: `minmax(20rem, min(34%, 36rem))` gives 320px
 * for any window where 34% is less than that, and this is one. Tall enough to clear
 * the 600px device-class threshold, so this is the two-region shell at its narrowest.
 */
const NARROWEST_TWO_REGION = { width: 700, height: 800 };
/** Wide, and short enough that the device-class rule gives it the stacked shell. */
const FULL_WIDTH = { width: 1280, height: 560 };
/** A comfortable landscape window. */
const LANDSCAPE = { width: 1280, height: 800 };

/** Narrows the shared corpus to this spec's own rows. */
const TOKEN = "gh238tree";

type Fixture = { id: string; release: () => Promise<void> };

/**
 * A parent and two children, returned with the calls that take them back out.
 *
 * Closed rather than deleted, and always: every spec in this directory shares one
 * server and one project, and `queue-order.spec.ts` drags between two rows that have
 * to sit in one scrollport together, so a fixture left open is a later spec's failure.
 */
async function seed(request: APIRequestContext) {
  const created: Array<Fixture> = [];
  const make = async (title: string, parent: string | null) => {
    const response = await request.post("/api/tasks", {
      data: {
        title,
        summary: "task-238 fixture.",
        description: "Exists so the sidebar tree has a parent with children in it.",
        lifecycle: "ready",
        category: "ux",
        priority: "low",
        actor: "E2E Human",
        ...(parent ? { parent } : {}),
      },
    });
    expect(response.ok()).toBeTruthy();
    const id = (await response.json()).id as string;
    created.push({
      id,
      async release() {
        const closed = await request.post(`/api/tasks/${id}/close`, {
          data: { actor: "E2E Human", outcome: "cancelled", body: "Tree fixture." },
        });
        expect(closed.ok()).toBeTruthy();
      },
    });
    return id;
  };
  const parent = await make(`${TOKEN} parent epic`, null);
  const first = await make(`${TOKEN} first child`, parent);
  const second = await make(`${TOKEN} second child`, parent);
  // Enough rows that the list region is taller than its scrollport whatever else this
  // shared project happens to hold, so the scroll test below runs rather than skipping
  // itself when this file is the only one executed. Deliberately without the token, so
  // every `?q=` filtered assertion above still sees exactly three rows.
  for (let index = 0; index < 24; index += 1) {
    await make(`Sidebar filler row ${index}`, null);
  }
  return {
    parent,
    first,
    second,
    // Children first: closing a parent with open children is not the shape this
    // fixture wants to leave behind.
    release: async () => {
      for (const fixture of [...created].reverse()) await fixture.release();
    },
  };
}

/** The rows on screen, top to bottom, narrowed to the ones this spec seeded. */
async function order(page: Page, seeded: Array<string>) {
  const rendered = await page
    .locator("[data-task]")
    .evaluateAll((rows) => rows.map((row) => row.getAttribute("data-task") ?? ""));
  return rendered.filter((id) => seeded.includes(id));
}

function disclosure(page: Page, taskId: string) {
  return page.getByRole("button", { name: new RegExp(`^(Fold|Unfold) ${taskId},`) });
}

/** What the browser says currently holds focus, as a poller. */
function focusedId(page: Page) {
  return () => page.evaluate(() => document.activeElement?.id ?? "");
}

/** The computed font size of a row's title, wherever that row is rendered. */
async function titleFontPx(page: Page, taskId: string) {
  return page.evaluate((id) => {
    const title = document.querySelector(`[data-task="${id}"] [title]`);
    if (!title) throw new Error(`No title for ${id}.`);
    return Number.parseFloat(getComputedStyle(title).fontSize);
  }, taskId);
}

test.describe("the task list as a sidebar tree", () => {
  let fixtures: Awaited<ReturnType<typeof seed>>;
  // Built here rather than from the `request` fixture: Playwright disposes that one
  // when `beforeAll` returns, so the `afterAll` that puts the corpus back would fail
  // on a closed context and leave three open rows in every later spec's list.
  let api: APIRequestContext;

  test.beforeAll(async ({ playwright, baseURL }) => {
    api = await playwright.request.newContext({ baseURL });
    fixtures = await seed(api);
  });

  test.afterAll(async () => {
    await fixtures.release();
    await api.dispose();
  });

  test("a row reads at 320px, at the same size the full-width table sets it", async ({ page }) => {
    // The full-width table first, so the comparison is against a measured number from
    // this build rather than against a constant in this file.
    await page.setViewportSize(FULL_WIDTH);
    await page.goto(`/app/p/_local/tasks?q=${TOKEN}`);
    await expect(page.locator(`[data-task="${fixtures.parent}"]`)).toBeVisible();
    const tablePx = await titleFontPx(page, fixtures.parent);

    await page.setViewportSize(NARROWEST_TWO_REGION);
    await page.goto(`/app/p/_local/tasks?q=${TOKEN}`);
    await expect(page.locator(`[data-task="${fixtures.parent}"]`)).toBeVisible();

    const column = await page.evaluate(() => {
      const region = document.querySelector('[data-region="list"]');
      return region ? Math.round(region.getBoundingClientRect().width) : null;
    });
    expect(column, "the list region did not render").not.toBeNull();
    // The floor of the column, which is what "reads at 320px" is a claim about.
    expect(column).toBeLessThanOrEqual(321);

    // The title has a box a person can read and click. Zero-width titles are the exact
    // failure task-237 hit when six fixed columns were squeezed into this region.
    const title = page.locator(`[data-task="${fixtures.parent}"] [title]`).first();
    const box = await title.boundingBox();
    expect(box, "the task title has no box at all").not.toBeNull();
    expect(box!.width, "the task title collapsed to nothing").toBeGreaterThan(140);

    // Nothing was shrunk to buy that width.
    expect(await titleFontPx(page, fixtures.parent)).toBeGreaterThanOrEqual(tablePx);

    // And the region absorbs its own width: the page does not scroll sideways.
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
      ),
    ).toBe(0);
  });

  test("folding a parent hides its children, says how many, and survives a reload", async ({
    page,
  }) => {
    await page.setViewportSize(LANDSCAPE);
    await page.goto(`/app/p/_local/tasks?q=${TOKEN}`);
    const seeded = [fixtures.parent, fixtures.first, fixtures.second];
    await expect.poll(() => order(page, seeded)).toEqual(seeded);

    const control = disclosure(page, fixtures.parent);
    await expect(control).toHaveAttribute("aria-expanded", "true");
    await control.click();

    await expect.poll(() => order(page, seeded)).toEqual([fixtures.parent]);
    await expect(control).toHaveAttribute("aria-expanded", "false");
    // The fold does not hide the work silently: the count is on the row itself.
    await expect(page.locator(`[data-task="${fixtures.parent}"]`)).toContainText("2 folded, 2 open");

    // The reload is the assertion. A fold held in component state would look identical
    // until this line, and the list refetches constantly.
    await page.reload();
    await expect.poll(() => order(page, seeded)).toEqual([fixtures.parent]);

    await disclosure(page, fixtures.parent).click();
    await expect.poll(() => order(page, seeded)).toEqual(seeded);
  });

  test("a deep link into a folded parent unfolds it and marks the row current", async ({
    page,
  }) => {
    await page.setViewportSize(LANDSCAPE);
    await page.goto(`/app/p/_local/tasks?q=${TOKEN}`);
    const seeded = [fixtures.parent, fixtures.first, fixtures.second];
    await expect.poll(() => order(page, seeded)).toEqual(seeded);
    await disclosure(page, fixtures.parent).click();
    await expect.poll(() => order(page, seeded)).toEqual([fixtures.parent]);

    // Pasted into the address bar with the fold still recorded, which is the case the
    // record calls out: it must reveal the task, not select something invisible.
    await page.goto(`/app/p/_local/tasks/${fixtures.second}?q=${TOKEN}`);

    await expect.poll(() => order(page, seeded)).toEqual(seeded);
    await expect(page.locator(`#task-row-${fixtures.second}`)).toHaveAttribute(
      "aria-current",
      "page",
    );
    await expect(page.locator('[data-region="detail"] h1')).toBeVisible();

    // Put the fold back, so this spec leaves the browser profile as it found it.
    await disclosure(page, fixtures.parent).click();
  });

  test("Up and Down move the selection and open that record beside the list", async ({ page }) => {
    await page.setViewportSize(LANDSCAPE);
    await page.goto(`/app/p/_local/tasks/${fixtures.parent}?q=${TOKEN}`);
    await expect(page.locator(`#task-row-${fixtures.parent}`)).toHaveAttribute(
      "aria-current",
      "page",
    );

    await page.locator(`#task-row-${fixtures.parent}`).focus();
    await page.keyboard.press("ArrowDown");
    // **Waiting for focus to arrive is not the same as putting it there.** task-207's
    // trap is a test that re-focuses the row before every press, which can never see
    // focus being lost; this asserts where focus actually landed and then presses again
    // without touching it. The wait is needed because `navigate` pushes the history
    // entry before React commits, so polling the URL alone can return a frame before
    // the row exists to hold focus -- which is a race in the harness, not in the app.
    await expect.poll(focusedId(page)).toBe(`task-row-${fixtures.first}`);
    expect(new URL(page.url()).pathname).toBe(`/app/p/_local/tasks/${fixtures.first}`);

    // Immediately again, and **without focusing anything first**: the selection moved
    // the focus with it, which is the property that makes a second press land on the
    // row the first one selected rather than on the row it started from.
    await page.keyboard.press("ArrowDown");
    await expect.poll(focusedId(page)).toBe(`task-row-${fixtures.second}`);
    expect(new URL(page.url()).pathname).toBe(`/app/p/_local/tasks/${fixtures.second}`);
    await expect(page.locator(`#task-row-${fixtures.second}`)).toHaveAttribute(
      "aria-current",
      "page",
    );
    // The record beside it followed, which is the whole reason the shape is worth having.
    await expect(page.locator('[data-region="detail"] h1')).toContainText("second child");
  });

  /**
   * The column's scroll position, under the two gestures that used to lose it.
   *
   * A short window, so the region has somewhere to scroll whatever this shared corpus
   * happens to hold at the time -- the claim is about the scrollport, and a list that
   * fits it cannot make the claim at all.
   */
  test("selecting a task leaves the column exactly where it was scrolled", async ({ page }) => {
    await page.setViewportSize({ width: 760, height: 620 });
    await page.goto("/app/p/_local/tasks");
    await expect(page.getByRole("region", { name: "Tasks" })).toBeVisible();

    const room = await page.evaluate(() => {
      const region = document.querySelector('[data-region="list"]')!;
      return region.scrollHeight - region.clientHeight;
    });
    test.skip(room < 400, "this project's list fits its region, so there is no scroll to keep");

    await page.evaluate(() => {
      document.querySelector('[data-region="list"]')!.scrollTop = 250;
    });

    // A click first: the defect this epic exists to remove used to unmount the list and
    // throw the offset away, and the tree must not have brought it back.
    const row = page.locator("[data-task] a[href*='/tasks/']").first();
    const href = await row.getAttribute("href");
    await row.click();
    await expect.poll(() => new URL(page.url()).pathname).toBe(href);
    await expect(page.locator('[data-region="detail"] h1')).toBeVisible();
    expect(
      await page.evaluate(() => document.querySelector('[data-region="list"]')!.scrollTop),
      "clicking a task scrolled the list",
    ).toBe(250);

    // And a keyboard step between two rows that are *both already on screen*, which is
    // the half of "scroll it into view" that is easy to get wrong: `block: "nearest"`
    // has to do nothing at all here. A row-into-view call that centred instead would
    // move the port on every press and make the list crawl under the reader.
    const start = await page.evaluate(() => {
      const region = document.querySelector('[data-region="list"]')!;
      const port = region.getBoundingClientRect();
      const rows = [...document.querySelectorAll("[data-task]")];
      for (let index = 0; index < rows.length - 1; index += 1) {
        const here = rows[index].getBoundingClientRect();
        const next = rows[index + 1].getBoundingClientRect();
        if (here.top >= port.top && next.bottom <= port.bottom) {
          return {
            from: rows[index].getAttribute("data-task"),
            to: rows[index + 1].getAttribute("data-task"),
          };
        }
      }
      return null;
    });
    expect(start, "no two adjacent rows were both on screen").not.toBeNull();

    await page.locator(`#task-row-${start!.from}`).focus();
    await page.keyboard.press("ArrowDown");
    await expect.poll(focusedId(page)).toBe(`task-row-${start!.to}`);
    expect(
      await page.evaluate(() => document.querySelector('[data-region="list"]')!.scrollTop),
      "moving the selection to a row already on screen scrolled the list",
    ).toBe(250);
  });
});
