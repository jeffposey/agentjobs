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
  // the row happens to sit.
  await expect(row).toHaveAttribute("data-queue-position", /^\d+$/);
  await expect(row.locator('[data-label="Queue"]')).toContainText(/\d+/);
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
async function dragOnto(page: Page, sourceId: string, targetId: string) {
  const grip = page.locator(`[id="queue-grip-${sourceId}"]`);
  const target = page.locator(`[data-task="${targetId}"] [data-label="Status"]`);
  // `page.mouse` takes viewport coordinates and scrolls nothing. Every spec in this
  // directory shares one server and one project, so by the time this runs the list
  // holds whatever earlier specs created and the rows this test seeded are below the
  // fold -- the mouse would then press on whatever happens to be at those coordinates
  // instead, and the test would report a broken drag. Both ends are scrolled into view
  // first, and the boxes are read only after all the scrolling is done.
  await target.scrollIntoViewIfNeeded();
  await grip.scrollIntoViewIfNeeded();
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
  const seeded = await seed(request, ["Drag first", "Drag second", "Drag third"]);
  const [first, second, third] = seeded;

  await page.goto("/app/p/_local/tasks");
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
  const [high] = await seed(request, ["Drag out of high"], "high");
  const [low] = await seed(request, ["Drag onto low"], "low");

  await page.goto("/app/p/_local/tasks");
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
test("scrolls the page while a drag is held at the bottom edge, and stops on release", async ({
  page,
  request,
}) => {
  // Enough rows that the document is taller than the window whatever else has been
  // seeded, and in `low` so this does not crowd the bands the drags above assert over.
  await seed(
    request,
    Array.from({ length: 30 }, (_, index) => `Autoscroll filler ${index}`),
    "low",
  );

  await page.goto("/app/p/_local/tasks");
  const grip = page.locator("[id^=queue-grip-]").first();
  await expect(grip).toBeVisible();
  await page.evaluate(() => window.scrollTo(0, 0));
  expect(
    await page.evaluate(() => document.documentElement.scrollHeight - window.innerHeight),
  ).toBeGreaterThan(200);

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
  await expect.poll(() => page.evaluate(() => window.scrollY)).toBeGreaterThan(100);

  await page.mouse.up();
  // Where the release happens to land is not this test's subject: the top row and the
  // rows at the bottom edge are in different bands, so the drop may raise the
  // confirmation. Clear it, so the panel appearing cannot be mistaken for the loop
  // still moving the page.
  const confirm = page.getByRole("alertdialog", { name: "Confirm a priority change" });
  if (await confirm.isVisible()) await confirm.getByRole("button", { name: "Cancel" }).click();

  const settled = await page.evaluate(() => window.scrollY);
  await page.waitForTimeout(300);
  expect(await page.evaluate(() => window.scrollY)).toBe(settled);
});
