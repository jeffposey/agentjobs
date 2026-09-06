import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

/**
 * Reordering the backlog with nothing but a keyboard, against the real server.
 *
 * Drag and drop is an accelerator here, not the path: it cannot be performed on the
 * phone and tablet this backlog is actually read from, and it cannot be driven without
 * a pointer harness. So the keyboard path is the one that has to keep working, and it
 * is the one covered — one assertion over the row's handler, the generated client, the
 * `queue-move` route, the queue lock and the YAML on disk together.
 *
 * The reload at the end is the part that matters most. An optimistic reorder makes any
 * gesture look like it worked; only a fresh page proves the server agreed.
 */

async function seed(request: APIRequestContext, titles: Array<string>, priority = "high") {
  const ids: Array<string> = [];
  for (const title of titles) {
    const response = await request.post("/api/tasks", {
      data: {
        title,
        description: "Seeded for the queue-order path.",
        summary: `Queue fixture: ${title}.`,
        priority,
        lifecycle: "ready",
        actor: "E2E Human",
      },
    });
    expect(response.ok()).toBeTruthy();
    ids.push((await response.json()).id);
  }
  return ids;
}

/**
 * The rows as rendered, top to bottom, narrowed to the ones a test seeded.
 *
 * Every spec in this directory shares one server and one project, so the band a test
 * seeds into already holds whatever earlier specs created. Narrowing keeps the
 * assertion about relative order -- which is the whole claim -- instead of about a
 * corpus this file does not own.
 */
async function order(page: Page, seeded: Array<string>) {
  const rendered = await page.locator("[data-task]").evaluateAll((rows) =>
    rows.map((row) => row.getAttribute("data-task") ?? ""),
  );
  return rendered.filter((id) => seeded.includes(id));
}

/**
 * Title fragments that narrow the list to one test's own rows.
 *
 * `?q=` searches title and id, so seeding a token and asking for it leaves a spec
 * looking at exactly the rows it created. Necessary since task-237 rather than merely
 * tidy: the list is a region of cards, so an unfiltered corpus puts far more pixels
 * between two seeded rows than one raw-mouse drag can span.
 */
const DRAG_TOKEN = "gh237drag";
const BAND_TOKEN = "gh237band";

function grip(page: Page, taskId: string) {
  return page.getByRole("button", { name: new RegExp(`^Reorder ${taskId},`) });
}

test("reorders the backlog from the keyboard, and the server keeps the new order", async ({
  page,
  request,
}) => {
  const seeded = await seed(request, ["Queue first", "Queue second", "Queue third"]);
  const [first, second, third] = seeded;

  await page.goto("/app/p/_local/tasks");
  // Creation puts a task at the bottom of its band, so they line up in the order filed.
  await expect.poll(() => order(page, seeded)).toEqual([first, second, third]);

  // Focus a row's handle and step it -- no pointer involved beyond reaching the page.
  await grip(page, third).focus();
  await page.keyboard.press("Alt+ArrowUp");
  await expect.poll(() => order(page, seeded)).toEqual([first, third, second]);

  // Immediately again, and **without focusing anything first**. Two presses is one
  // gesture as far as a person is concerned, so the second must land on the same task
  // -- the row moved underneath the focused handle, and a browser drops focus from a
  // node that is reinserted. Re-focusing here would hide exactly that, which is how
  // this got past a green suite once already.
  await page.keyboard.press("Alt+ArrowUp");
  await expect.poll(() => order(page, seeded)).toEqual([third, first, second]);

  // The reload is the assertion. Everything above would look identical if the move had
  // only ever happened in the browser.
  await page.reload();
  await expect.poll(() => order(page, seeded)).toEqual([third, first, second]);

  // And each decision is on the record, not merely in the file's position field.
  const record = await (await request.get(`/api/tasks/${third}`)).json();
  const moves = record.log.filter((entry: { type: string }) => entry.type === "queue_move");
  expect(moves).toHaveLength(2);
  expect(record.queue_position).toBeLessThan(
    (await (await request.get(`/api/tasks/${first}`)).json()).queue_position,
  );
});

test("a step that would not move anything writes nothing", async ({ page, request }) => {
  // Creation appends to the bottom of the band, so the newest task is last in line
  // whatever else this shared project already holds.
  const [, last] = await seed(request, ["Second from last", "Last in the band"]);

  await page.goto("/app/p/_local/tasks");
  await grip(page, last).focus();
  await page.keyboard.press("Alt+ArrowDown");
  await page.keyboard.press("Alt+End");

  // No `queue_move` entry, because nothing moved. A move that lands a task exactly
  // where it already is still records a decision, and nobody made this one.
  const record = await (await request.get(`/api/tasks/${last}`)).json();
  expect(record.log.filter((entry: { type: string }) => entry.type === "queue_move")).toHaveLength(0);
});

test("shows the position it is about to change", async ({ page, request }) => {
  const [first] = await seed(request, ["Positioned", "Second in line"]);

  await page.goto("/app/p/_local/tasks");
  const row = page.locator(`[data-task="${first}"]`);
  // The number a person is changing, rendered as a value rather than implied by where
  // the row happens to sit. `data-field` rather than the table's `data-label`, because
  // since task-238 the same row renders as a tree row in the sidebar and as a cell in
  // the full-width table, and the claim is about both.
  await expect(row).toHaveAttribute("data-queue-position", /^\d+$/);
  await expect(row.locator('[data-field="queue"]')).toContainText(/\d+/);
});

// The dashboard's "Why this one?" disclosure is deliberately not covered here. Which
// panel the dashboard renders is decided by a ladder over the *whole* project, and this
// directory shares one project across every spec -- so whether the "Next up" rung is on
// screen depends on what the specs that ran earlier happened to create. A test that
// asserts it passes alone and fails in the suite, which is exactly what it did. It is
// covered instead by NextExplanation.test.tsx against the real endpoint's shape, and it
// was exercised by hand in a browser against a seeded sandbox (task-207 log).

/**
 * Drag, driven by a real mouse rather than a synthesised `dragstart`.
 *
 * task-207 covered dragging with `fireEvent.dragStart` in jsdom and with nothing at all
 * in Playwright. A synthetic `dragstart` proves the handler does the right arithmetic
 * *once the browser has decided to start a drag*; it cannot prove the browser will start
 * one, and "the browser never starts one" was the reported defect. So these two press,
 * move and release the mouse and let Chromium decide, which is the only part the older
 * tests could not reach.
 *
 * Read what they are and are not evidence for. Playwright drives Chromium's drag through
 * `Input.setInterceptDrags`, so this is the browser's own drag controller deciding
 * whether the handle is a drag source, but it is not the operating system's drag loop.
 * These catch a regression in the element, the handlers, the client call and the route.
 * They cannot stand in for a hand on a mouse -- see task-225.
 */
/**
 * The box the rows scroll inside, as an expression evaluated in the page.
 *
 * The list's own region on the two-region Tasks surface (task-237), where the document
 * does not scroll at all, and `null` -- meaning the window -- in the stacked shell.
 * Written as a string because three helpers below need it inside their own
 * `page.evaluate`, and a function declared out here is not in scope in there.
 */
const FIND_SCROLLER = `(from) => {
  let candidate = from ? from.parentElement : null;
  while (candidate && candidate !== document.body && candidate !== document.documentElement) {
    const overflow = getComputedStyle(candidate).overflowY;
    if ((overflow === "auto" || overflow === "scroll")
        && candidate.scrollHeight > candidate.clientHeight) return candidate;
    candidate = candidate.parentElement;
  }
  return null;
}`;

/** How far the rows have been scrolled, wherever it is they scroll. */
async function listScrollTop(page: Page) {
  return page.evaluate(`(() => {
    const findScroller = ${FIND_SCROLLER};
    const scroller = findScroller(document.querySelector("[data-task]"));
    return scroller ? scroller.scrollTop : window.scrollY;
  })()`) as Promise<number>;
}

/** How much room those rows have left to scroll into. Zero means the list fits. */
async function listScrollRoom(page: Page) {
  return page.evaluate(`(() => {
    const findScroller = ${FIND_SCROLLER};
    const scroller = findScroller(document.querySelector("[data-task]"));
    if (scroller) return scroller.scrollHeight - scroller.clientHeight;
    return document.documentElement.scrollHeight - window.innerHeight;
  })()`) as Promise<number>;
}

async function dragOnto(page: Page, sourceId: string, targetId: string) {
  const grip = page.locator(`[id="queue-grip-${sourceId}"]`);
  const target = page.locator(`[data-task="${targetId}"] [data-field="status"]`);
  // `page.mouse` takes viewport coordinates and scrolls nothing, so both ends of the
  // gesture have to be on screen -- and clear of the pinned header (task-292), which
  // covers the top 65px of every page. Two `scrollIntoViewIfNeeded` calls used to do
  // this and they cannot: each scrolls the minimum, so the second undoes the first
  // whenever the rows are far apart, and the minimum for a row above the fold puts it
  // at y=0, underneath the header, where the press lands on the header instead. Both
  // failure modes were observed; this places the pair deliberately instead.
  //
  // Which box to move is now a question (task-237). On the two-region shell the rows
  // scroll inside the list's region and the window scrolls nowhere, so scrolling the
  // window would place nothing and leave the pair wherever it found them. The region
  // also starts below the header, so nothing has to be subtracted for the bar there.
  const placed = await page.evaluate(
    ([gripId, taskId, findScrollerSource]) => {
      const findScroller = new Function(`return (${findScrollerSource})`)() as (
        from: Element | null,
      ) => Element | null;
      const gripElement = document.getElementById(gripId as string);
      const targetElement = document.querySelector(
        `[data-task="${taskId}"] [data-field="status"]`,
      );
      const header = document.querySelector("header");
      if (!gripElement || !targetElement || !header) return null;
      const boxes = [gripElement.getBoundingClientRect(), targetElement.getBoundingClientRect()];
      const top = Math.min(...boxes.map((box) => box.top));
      const bottom = Math.max(...boxes.map((box) => box.bottom));
      const scroller = findScroller(gripElement);
      if (scroller) {
        const port = scroller.getBoundingClientRect();
        const usable = scroller.clientHeight;
        // Centre the pair in the region, in the region's own coordinates.
        scroller.scrollTop += (top + bottom) / 2 - (port.top + usable / 2);
        return { span: bottom - top, usable };
      }
      const headerHeight = header.getBoundingClientRect().height;
      const usable = window.innerHeight - headerHeight;
      // Centre the pair in the band the header leaves behind.
      window.scrollTo(
        0,
        Math.max(0, (top + bottom) / 2 + window.scrollY - headerHeight - usable / 2),
      );
      return { span: bottom - top, usable };
    },
    [`queue-grip-${sourceId}`, targetId, FIND_SCROLLER],
  );
  if (!placed) throw new Error(`No grip or target for ${sourceId} -> ${targetId}.`);
  if (placed.span > placed.usable) {
    // Said out loud rather than left to surface as a drag that silently did nothing.
    // A raw-mouse drag cannot reach across more than one screen; the feature handles
    // it by auto-scrolling at the edge, which is a different test.
    throw new Error(
      `${sourceId} and ${targetId} are ${Math.round(placed.span)}px apart, more than the ` +
        `${Math.round(placed.usable)}px this scrollport leaves, so one raw-mouse drag ` +
        "cannot span them. Seed fewer rows between them, or use a taller viewport.",
    );
  }
  const from = await grip.boundingBox();
  const onto = await target.boundingBox();
  if (!from || !onto) throw new Error(`No box for ${sourceId} -> ${targetId}.`);
  await page.mouse.move(from.x + from.width / 2, from.y + from.height / 2);
  await page.mouse.down();
  // Two moves after the press, deliberately. Chromium starts a drag on the *second*
  // move, so a single jump to the target releases the button before a drag ever begins
  // and the test would report a broken feature that works.
  await page.mouse.move(onto.x + onto.width / 2, onto.y + onto.height / 2, { steps: 15 });
  await page.mouse.move(onto.x + onto.width / 2 + 3, onto.y + onto.height / 2 + 3, { steps: 5 });
  await page.mouse.up();
}

test("drags one task onto another with a real mouse, and the server keeps the order", async ({
  page,
  request,
}) => {
  // Taller than the 720px default, because a raw-mouse drag needs both rows on screen
  // at once and task-292's pinned header now takes 65px off the top of every page.
  // The height is a property of the harness, not a claim about the product: the
  // gesture a person makes across a longer list is the auto-scroll one, covered below.
  await page.setViewportSize({ width: 1280, height: 900 });
  const seeded = await seed(request, [
    `${DRAG_TOKEN} first`,
    `${DRAG_TOKEN} second`,
    `${DRAG_TOKEN} third`,
  ]);
  const [first, second, third] = seeded;

  // Filtered to this spec's own rows. Not tidiness: the list is a region a third of
  // the screen wide since task-237 and its rows are cards there, so whatever else the
  // shared corpus holds between two seeded tasks is now four times as many pixels as
  // it used to be, and a raw-mouse drag cannot reach across more than one scrollport.
  await page.goto(`/app/p/_local/tasks?q=${DRAG_TOKEN}`);
  await expect.poll(() => order(page, seeded)).toEqual([first, second, third]);

  await dragOnto(page, third, first);
  await expect.poll(() => order(page, seeded)).toEqual([third, first, second]);

  // The reload is the assertion, exactly as it is for the keyboard path above:
  // everything before it would look identical if the move had only ever been optimistic.
  await page.reload();
  await expect.poll(() => order(page, seeded)).toEqual([third, first, second]);

  const record = await (await request.get(`/api/tasks/${third}`)).json();
  expect(
    record.log.filter((entry: { type: string }) => entry.type === "queue_move"),
  ).toHaveLength(1);
});

test("a cross-band drag asks before it reprioritises", async ({ page, request }) => {
  // Taller than the 720px default, because a raw-mouse drag needs both rows on screen
  // at once and task-292's pinned header now takes 65px off the top of every page.
  await page.setViewportSize({ width: 1280, height: 900 });
  const [high] = await seed(request, [`${BAND_TOKEN} out of high`], "high");
  const [low] = await seed(request, [`${BAND_TOKEN} onto low`], "low");

  // Filtered, and here it is load-bearing rather than a precaution: these two are at
  // opposite ends of the queue, so unfiltered the whole backlog lies between them --
  // 2794px of it once the list became a region of cards (task-237).
  await page.goto(`/app/p/_local/tasks?q=${BAND_TOKEN}`);
  await expect.poll(() => order(page, [high, low])).toEqual([high, low]);

  await dragOnto(page, high, low);

  // Two decisions in one gesture, so the second is asked out loud. Nothing has been
  // written yet at this point.
  const confirm = page.getByRole("alertdialog", { name: "Confirm a priority change" });
  await expect(confirm).toBeVisible();
  await expect(confirm).toContainText(high);
  expect((await (await request.get(`/api/tasks/${high}`)).json()).priority).toBe("high");

  await confirm.getByRole("button", { name: "Move it to low" }).click();

  // And it is a reprioritise, not a move: the band is what changed.
  await expect
    .poll(async () => (await (await request.get(`/api/tasks/${high}`)).json()).priority)
    .toBe("low");
  await page.reload();
  await expect.poll(() => order(page, [high, low])).toEqual([high, low]);
});

/**
 * The page scrolls while a drag is held at an edge -- the one claim jsdom cannot make.
 *
 * `dragAutoScroll.test.ts` drives the loop with a hand-turned frame clock and a fake
 * scroller, which settles the arithmetic and the teardown but says nothing about
 * whether a real browser fires `dragover` at the document during a drag, or whether
 * `window.scrollBy` moves this page. That is what this covers.
 *
 * It is still not the acceptance evidence for "a person can now reach an off-screen
 * row": Playwright's drag goes in through `Input.setInterceptDrags`, below the
 * operating system's drag loop, and task-225 is the incident that says what happens
 * when that distinction is forgotten. A hand on a mouse in the seeded sandbox is the
 * evidence for that, and it is recorded on task-229.
 */
test("scrolls the list while a drag is held at the bottom edge, and stops on release", async ({
  page,
  request,
}) => {
  // Enough rows that the list is taller than its scrollport whatever else has been
  // seeded, and in `low` so this does not crowd the bands the drags above assert over.
  await seed(
    request,
    Array.from({ length: 30 }, (_, index) => `Autoscroll filler ${index}`),
    "low",
  );

  await page.goto("/app/p/_local/tasks");
  const grip = page.locator("[id^=queue-grip-]").first();
  await expect(grip).toBeVisible();
  // Which box scrolls is the thing task-237 changed, and the whole claim here is that
  // the loop moves whichever one it is. Asked of the page rather than assumed.
  await page.evaluate(`(() => {
    const findScroller = ${FIND_SCROLLER};
    const scroller = findScroller(document.querySelector("[data-task]"));
    if (scroller) scroller.scrollTop = 0; else window.scrollTo(0, 0);
  })()`);
  expect(await listScrollRoom(page)).toBeGreaterThan(200);

  const box = await grip.boundingBox();
  const viewport = page.viewportSize();
  if (!box || !viewport) throw new Error("No grip box or viewport.");

  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
  // Two moves, because Chromium starts the drag on the second one. Both land inside
  // the bottom edge zone, which is where the loop is supposed to take over.
  await page.mouse.move(box.x + box.width / 2, viewport.height - 6, { steps: 15 });
  await page.mouse.move(box.x + box.width / 2 + 2, viewport.height - 4, { steps: 5 });

  // Held still from here on. The loop must keep scrolling from the last reading rather
  // than needing a stream of events, because a held hand does not produce one.
  await expect.poll(() => listScrollTop(page)).toBeGreaterThan(100);

  await page.mouse.up();
  // Where the release happens to land is not this test's subject: the top row and the
  // rows at the bottom edge are in different bands, so the drop may raise the
  // confirmation. Clear it, so the panel appearing cannot be mistaken for the loop
  // still moving the page.
  const confirm = page.getByRole("alertdialog", { name: "Confirm a priority change" });
  if (await confirm.isVisible()) await confirm.getByRole("button", { name: "Cancel" }).click();

  const settled = await listScrollTop(page);
  await page.waitForTimeout(300);
  expect(await listScrollTop(page)).toBe(settled);
});
