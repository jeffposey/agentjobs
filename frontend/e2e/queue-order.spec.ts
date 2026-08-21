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

async function seed(request: APIRequestContext, titles: Array<string>) {
  const ids: Array<string> = [];
  for (const title of titles) {
    const response = await request.post("/api/tasks", {
      data: {
        title,
        description: "Seeded for the queue-order path.",
        summary: `Queue fixture: ${title}.`,
        priority: "high",
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
