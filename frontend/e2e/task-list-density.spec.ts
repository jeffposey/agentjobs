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
 */

const DESKTOP = { width: 1280, height: 800 };
/** The narrow end of the table layout: below 820px the rows become cards instead. */
const NARROW_DESKTOP = { width: 840, height: 800 };
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

/** The rows this spec put on screen, with the height a person would see. */
async function rowHeights(page: Page) {
  return page.evaluate(() =>
    [...document.querySelectorAll("tbody tr")].map((row) => ({
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
    await page.setViewportSize(DESKTOP);
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
        DESKTOP.width,
      );
    }

    for (const row of await rowHeights(page)) {
      expect(row.height, `${row.task} is ${row.height}px tall`).toBeLessThanOrEqual(ROW_MAX_PX);
    }
  });

  test("a long title is cut inside its column instead of widening it", async ({ page }) => {
    await page.setViewportSize(DESKTOP);
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
    await page.setViewportSize(NARROW_DESKTOP);
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
});
