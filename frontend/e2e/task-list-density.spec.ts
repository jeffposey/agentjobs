import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

/**
 * task-341: the task list fits its scrollport, and no one task's prose sizes a row.
 *
 * The reported symptom was that the first screen of `/p/<project>/tasks` rendered as
 * an empty void. Two separate causes, and the reason both are asserted here rather
 * than in jsdom is that neither exists until a browser lays the table out:
 *
 *  - `table-layout: auto` let one long title claim 1261px of a 1197px viewport, so
 *    Status, Priority, Assigned and Updated sat off the right-hand edge behind a
 *    horizontal scrollbar. jsdom has no table algorithm at all; a unit test asserting
 *    `class="truncate"` passed throughout, because the class was always there and had
 *    never had a bounded box to work in.
 *  - A `ball_prompt` written for a task detail page, poured into the Status column,
 *    wrapped to 2070px and made its row 2091px tall against a median of 77px.
 *
 * So both assertions are measured rectangles from a real browser. ENGINEERING.md,
 * Verification.
 *
 * **The viewports moved in task-237 and the claims did not.** The task list is now a
 * region on a two-region surface at any viewport 600px on its short side, and six
 * columns totalling 39.5rem do not fit a region a third of a screen wide -- so there
 * the rows are cards, and "every column is on screen" is a claim about the card, not
 * about a table. The table form is still exactly what a full-width list renders, and
 * still what these three tests are about; they ask for it at a *short* window, which
 * the device-class rule treats as a phone and gives the stacked shell. The two-region
 * geometry has its own test at the bottom of this file.
 */

/** Wide, and short enough that the device-class rule gives it the stacked shell. */
const FULL_WIDTH = { width: 1280, height: 560 };
/** The narrow end of the table layout: below 820px the rows become cards instead. */
const NARROW_FULL_WIDTH = { width: 840, height: 560 };
/** A landscape window that gets the list and a record side by side. */
const TWO_REGION = { width: 1280, height: 800 };
/** iPhone 14/15 CSS pixels, which is where this app is read over Tailscale. */
const PHONE = { width: 390, height: 844 };

/**
 * A row is the Task cell's three lines plus its padding. 77px was the median of the
 * 122 real rows measured when this was filed; 160px leaves room for a wrapped id or a
 * two-line status without leaving room for a paragraph.
 */
const ROW_MAX_PX = 160;

/**
 * Shared by both fixture titles, so `?q=` narrows the list to exactly this spec's two
 * rows. The measurement wants the fixtures on screen, not the whole backlog, and
 * every other spec in this directory shares this project's corpus.
 */
const TOKEN = "gh341density";

/** Long enough to have sized the column on its own, from the real task that did. */
const LONG_TITLE =
  `${TOKEN} the gate is launched six to nine times per task at seven to twelve ` +
  "minutes each under contention: one gate per handoff, and a core budget shared " +
  "between the runs that overlap";

/** A real review request is several paragraphs. This is one of them, three times. */
const LONG_PROMPT =
  "Branch feat/task-000-a-worked-example, five commits, gate green on the rebased " +
  "tree with a receipt for the commit under it. Every acceptance criterion is met " +
  "and evidenced on the record. Read the diff, then approve or say what is missing. "
    .repeat(3);

type Fixture = { id: string; release: () => Promise<void> };

/**
 * A task in the open list, returned with the call that takes it back out.
 *
 * Closed rather than deleted, and always: every spec here shares one server and one
 * project, and `queue-order.spec.ts` drags between two rows that have to sit in one
 * viewport together -- so a fixture left open is a later spec's failure.
 */
async function fixture(
  request: APIRequestContext,
  title: string,
  prompt: string | null,
): Promise<Fixture> {
  const created = await request.post("/api/tasks", {
    data: {
      title,
      summary: "Exists so the task list has a row that used to break its layout.",
      description: "task-341 fixture.",
      lifecycle: "ready",
      category: "ux",
      actor: "E2E Human",
    },
  });
  expect(created.ok()).toBeTruthy();
  const id = (await created.json()).id as string;

  if (prompt !== null) {
    const handed = await request.post(`/api/tasks/${id}/handoff`, {
      data: { actor: "E2E Human", ball: "human", ball_reason: "review", ball_prompt: prompt },
    });
    expect(handed.ok()).toBeTruthy();
  }

  return {
    id,
    async release() {
      const closed = await request.post(`/api/tasks/${id}/close`, {
        data: { actor: "E2E Human", outcome: "cancelled", body: "Layout fixture." },
      });
      expect(closed.ok()).toBeTruthy();
    },
  };
}

/**
 * The rows this spec put on screen, with the height a person would see.
 *
 * By `[data-task]` rather than by `tbody tr`, because since task-238 the same rows are
 * a table in the full-width shell and a list in the sidebar, and every claim in this
 * file is about the row rather than about the element it happens to be.
 */
async function rowHeights(page: Page) {
  return page.evaluate(() =>
    [...document.querySelectorAll("[data-task]")].map((row) => ({
      task: (row as HTMLElement).dataset.task ?? "",
      height: Math.round(row.getBoundingClientRect().height),
    })),
  );
}

/** How far the table overflows the box it is scrolling inside. Zero, or it does not fit. */
async function overflowPx(page: Page) {
  return page.evaluate(() => {
    const table = document.querySelector(".responsive-table");
    const wrap = document.querySelector(".responsive-table-wrap");
    if (!table || !wrap) throw new Error("The task table did not render.");
    return Math.max(0, Math.round(table.scrollWidth - wrap.clientWidth));
  });
}

test.describe("the task list fits the screen it is on", () => {
  let fixtures: Array<Fixture> = [];
  // Built here rather than taken from the `request` fixture: Playwright disposes that
  // one when `beforeAll` returns, so the `afterAll` that puts the corpus back would
  // fail on a closed context and leave two open rows in every later spec's list.
  let api: APIRequestContext;

  test.beforeAll(async ({ playwright, baseURL }) => {
    api = await playwright.request.newContext({ baseURL });
    fixtures = [
      await fixture(api, LONG_TITLE, null),
      await fixture(api, `${TOKEN} parked on review with a paragraph to say why`, LONG_PROMPT),
    ];
  });

  test.afterAll(async () => {
    for (const created of fixtures) await created.release();
    await api.dispose();
  });

  test("every column is on screen, and no row is sized by a ball_prompt", async ({ page }) => {
    await page.setViewportSize(FULL_WIDTH);
    await page.goto(`/app/p/_local/tasks?q=${TOKEN}`);
    await expect(page.locator(`tbody tr[data-task="${fixtures[0].id}"]`)).toBeVisible();

    expect(await overflowPx(page)).toBe(0);

    // Not "a scrollbar is absent" -- the columns being reachable is the claim, and a
    // column can be clipped by an ancestor with no scrollbar anywhere in sight.
    for (const label of ["Queue", "Task", "Status", "Priority", "Assigned", "Updated"]) {
      const cell = page.locator(`tbody tr[data-task="${fixtures[0].id}"] td[data-label="${label}"]`);
      const box = await cell.boundingBox();
      expect(box, `${label} has no box`).not.toBeNull();
      expect(box!.x, `${label} starts off the left edge`).toBeGreaterThanOrEqual(0);
      expect(box!.x + box!.width, `${label} runs past the right edge`).toBeLessThanOrEqual(
        FULL_WIDTH.width,
      );
    }

    for (const row of await rowHeights(page)) {
      expect(row.height, `${row.task} is ${row.height}px tall`).toBeLessThanOrEqual(ROW_MAX_PX);
    }
  });

  test("a long title is cut inside its column instead of widening it", async ({ page }) => {
    await page.setViewportSize(FULL_WIDTH);
    await page.goto(`/app/p/_local/tasks?q=${TOKEN}`);
    const title = page.locator(`tbody tr[data-task="${fixtures[0].id}"] [title]`).first();
    await expect(title).toBeVisible();

    // The rendered box is narrower than the text it holds, which is what an ellipsis
    // means. Asserting the class instead would have passed before the fix.
    const cut = await title.evaluate((node) => ({
      clientWidth: node.clientWidth,
      scrollWidth: node.scrollWidth,
      full: node.getAttribute("title") ?? "",
      shown: (node.textContent ?? "").trim(),
    }));
    expect(cut.scrollWidth).toBeGreaterThan(cut.clientWidth);
    // Cut on screen, whole in the tooltip: the text is shortened, never lost.
    expect(cut.full).toBe(LONG_TITLE);
    expect(cut.shown).toBe(LONG_TITLE);
  });

  test("the columns still fit at the narrow end of the table layout", async ({ page }) => {
    await page.setViewportSize(NARROW_FULL_WIDTH);
    await page.goto(`/app/p/_local/tasks?q=${TOKEN}`);
    await expect(page.locator(`tbody tr[data-task="${fixtures[0].id}"]`)).toBeVisible();

    expect(await overflowPx(page)).toBe(0);
    for (const row of await rowHeights(page)) {
      expect(row.height, `${row.task} is ${row.height}px tall`).toBeLessThanOrEqual(ROW_MAX_PX);
    }
  });

  test("the phone still gets cards, and the column widths stay out of them", async ({ page }) => {
    await page.setViewportSize(PHONE);
    await page.goto(`/app/p/_local/tasks?q=${TOKEN}`);
    const row = page.locator(`tbody tr[data-task="${fixtures[0].id}"]`);
    await expect(row).toBeVisible();

    // Below 820px the rows are cards: the table is a block, the header row is gone,
    // and each cell prints its own label. A `<colgroup>` sized for six columns has no
    // business in that layout, and this is the assertion that says it stayed out --
    // the widths are hidden rather than merely ignored.
    const layout = await page.evaluate(() => {
      const table = document.querySelector(".responsive-table")!;
      const group = document.querySelector("colgroup");
      const cell = document.querySelector("tbody td")!;
      return {
        table: getComputedStyle(table).display,
        head: getComputedStyle(document.querySelector("thead")!).display,
        group: group ? getComputedStyle(group).display : "absent",
        cell: getComputedStyle(cell).display,
        label: getComputedStyle(cell, "::before").content,
      };
    });
    expect(layout.table).toBe("block");
    expect(layout.head).toBe("none");
    expect(layout.group).toBe("none");
    expect(layout.cell).toBe("grid");
    expect(layout.label).toContain("Queue");

    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
      ),
    ).toBe(0);
  });

  /**
   * task-238: what the same rows do once the list is a region rather than the page.
   *
   * The regression this guards started as a layout failure and became a component
   * choice. A `table-layout: fixed` table gives its one flexible column whatever the
   * fixed ones leave over, and five fixed columns asking for 39.5rem leave a ~430px
   * region nothing at all -- so the task title collapsed to zero pixels, Chromium
   * reported the cell as `hidden`, and every spec that clicks a task by its title
   * failed at once while the page still looked populated. task-237 restacked the rows
   * into cards to stop the bleeding; task-238 replaced them with a tree, which is what
   * a master column actually wants. What has to stay true through both is the same
   * sentence: **every row has a title with a real box, and the region absorbs its own
   * width.**
   *
   * The row heights are asserted here and were not under the cards, because that was
   * the thing task-237 could not hand this task: six labelled lines per row is a card,
   * and a column you pick from is a row.
   */
  test("the rows become a tree once the list is a region, with a title in every one", async ({
    page,
  }) => {
    await page.setViewportSize(TWO_REGION);
    await page.goto(`/app/p/_local/tasks?q=${TOKEN}`);
    const row = page.locator(`[data-task="${fixtures[0].id}"]`);
    await expect(row).toBeVisible();

    // A list, not a table: the six-column grid is gone from this region entirely, so
    // there is nothing left to squeeze a title to zero.
    expect(
      await page.evaluate(() => document.querySelectorAll('[data-region="list"] table').length),
    ).toBe(0);

    // The defect in one assertion: the title has a box a person can see and click.
    const title = page.locator(`[data-task="${fixtures[0].id}"] [title]`).first();
    await expect(title).toBeVisible();
    const box = await title.boundingBox();
    expect(box, "the task title has no box at all").not.toBeNull();
    expect(box!.width, "the task title collapsed to nothing").toBeGreaterThan(100);

    // And no row is sized by a `ball_prompt` here either. The second fixture is parked
    // on review with three paragraphs in its prompt; the tree row does not print it.
    for (const measured of await rowHeights(page)) {
      expect(measured.height, `${measured.task} is ${measured.height}px tall`).toBeLessThanOrEqual(
        ROW_MAX_PX,
      );
    }

    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
      ),
    ).toBe(0);
  });
});
