import { expect, test, type APIRequestContext, type CDPSession, type Page } from "./fixtures";

/**
 * Reordering the backlog by touch, on a tablet-sized, touch-enabled page (task-589).
 *
 * The grip used to be an HTML5 `draggable` and nothing else. Touch browsers do not
 * start an HTML5 drag from a finger -- or start one only after a long-press, and then
 * fight the page scroll for it -- so on the tablet the backlog is read from there was
 * no way to reorder by hand at all.
 *
 * **The gesture is real touch input, not a handler called by name.** Every touch here
 * goes in through CDP's `Input.dispatchTouchEvent`, which is the browser's own input
 * pipeline: it hit-tests, applies `touch-action`, derives pointer events and decides
 * whether a finger is scrolling. That is the part the older reorder tests could not
 * reach (task-207, task-225), and it is the part that was broken here. What it is still
 * not is a hand on a glass screen; the review sandbox is for that.
 */

test.use({ viewport: { width: 820, height: 1180 }, hasTouch: true, isMobile: true });

const TOKEN = "gh589touch";

async function seed(request: APIRequestContext, titles: Array<string>, priority = "high") {
  const ids: Array<string> = [];
  for (const title of titles) {
    const response = await request.post("/api/tasks", {
      data: {
        title,
        description: "Seeded for the touch reorder path.",
        summary: `Touch fixture: ${title}.`,
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

async function order(page: Page, seeded: Array<string>) {
  const rendered = await page
    .locator("[data-task]")
    .evaluateAll((rows) => rows.map((row) => row.getAttribute("data-task") ?? ""));
  return rendered.filter((id) => seeded.includes(id));
}

/** How far the rows have scrolled, wherever it is they scroll. */
async function listScrollTop(page: Page) {
  return page.evaluate(() => {
    let candidate = document.querySelector("[data-task]")?.parentElement ?? null;
    while (candidate && candidate !== document.body && candidate !== document.documentElement) {
      const overflow = getComputedStyle(candidate).overflowY;
      if ((overflow === "auto" || overflow === "scroll") && candidate.scrollHeight > candidate.clientHeight) {
        return candidate.scrollTop;
      }
      candidate = candidate.parentElement;
    }
    return window.scrollY;
  });
}

/** One finger, driven through the browser's input pipeline. */
class Finger {
  private constructor(private readonly cdp: CDPSession) {}

  static async on(page: Page) {
    return new Finger(await page.context().newCDPSession(page));
  }

  private async send(type: "touchStart" | "touchMove" | "touchEnd", x: number, y: number) {
    await this.cdp.send("Input.dispatchTouchEvent", {
      type,
      touchPoints: type === "touchEnd" ? [] : [{ x: Math.round(x), y: Math.round(y), id: 1 }],
    });
  }

  private x = 0;
  private y = 0;

  async down(x: number, y: number) {
    this.x = x;
    this.y = y;
    await this.send("touchStart", x, y);
  }

  /** Slide to (x, y) in `steps` moves, the way a finger actually travels. */
  async move(x: number, y: number, steps = 12) {
    const [fromX, fromY] = [this.x, this.y];
    for (let step = 1; step <= steps; step += 1) {
      this.x = fromX + ((x - fromX) * step) / steps;
      this.y = fromY + ((y - fromY) * step) / steps;
      await this.send("touchMove", this.x, this.y);
    }
  }

  async up() {
    await this.send("touchEnd", this.x, this.y);
  }
}

async function centre(page: Page, selector: string) {
  const box = await page.locator(selector).boundingBox();
  if (!box) throw new Error(`No box for ${selector}.`);
  return { x: box.x + box.width / 2, y: box.y + box.height / 2 };
}

test("drags a task by its grip with a finger, and the server keeps the order", async ({
  page,
  request,
}) => {
  const seeded = await seed(request, [`${TOKEN} first`, `${TOKEN} second`, `${TOKEN} third`]);
  const [first, second, third] = seeded;

  await page.goto(`/app/p/_local/tasks?q=${TOKEN}`);
  await expect.poll(() => order(page, seeded)).toEqual([first, second, third]);

  const from = await centre(page, `[id="queue-grip-${third}"]`);
  const onto = await centre(page, `[data-task="${first}"] [data-field="status"]`);
  const sourceBox = await page.locator(`[data-task="${third}"]`).boundingBox();
  if (!sourceBox) throw new Error("No source row box.");

  const finger = await Finger.on(page);
  await finger.down(from.x, from.y);
  await finger.move(onto.x, onto.y);

  // Mid-gesture: the row in flight is marked, and the insertion line is drawn on the
  // row it would land beside. The same two attributes a mouse drag sets (task-365).
  await expect(page.locator(`[data-task="${third}"]`)).toHaveAttribute("data-dragging", "true");
  await expect(page.locator(`[data-task="${first}"]`)).toHaveAttribute("data-drop-side", "before");

  // And a picture of the row travels under the finger, as the browser's drag image does
  // under a mouse. Held where it was taken: the finger went down on the grip, so the
  // grip's place in the ghost is still under the finger.
  const ghost = page.locator("[data-drag-ghost]");
  await expect(ghost).toHaveCount(1);
  await expect(ghost).toContainText(`${TOKEN} third`);
  const ghostBox = await ghost.boundingBox();
  if (!ghostBox) throw new Error("No ghost box.");
  expect(Math.abs(ghostBox.y - (onto.y - (from.y - sourceBox.y)))).toBeLessThan(3);
  expect(Math.abs(ghostBox.x - (onto.x - (from.x - sourceBox.x)))).toBeLessThan(3);

  await finger.up();
  await expect(ghost).toHaveCount(0);
  await expect.poll(() => order(page, seeded)).toEqual([third, first, second]);
  await expect(page.locator("[data-dragging]")).toHaveCount(0);
  await expect(page.locator("[data-drop-side]")).toHaveCount(0);

  // The reload is the assertion: an optimistic reorder looks the same until then.
  await page.reload();
  await expect.poll(() => order(page, seeded)).toEqual([third, first, second]);

  const record = await (await request.get(`/api/tasks/${third}`)).json();
  expect(
    record.log.filter((entry: { type: string }) => entry.type === "queue_move"),
  ).toHaveLength(1);
});

test("a tap on the grip moves nothing", async ({ page, request }) => {
  const seeded = await seed(request, [`${TOKEN}tap first`, `${TOKEN}tap second`]);
  const [first, second] = seeded;
  await page.goto(`/app/p/_local/tasks?q=${TOKEN}tap`);
  await expect.poll(() => order(page, seeded)).toEqual([first, second]);

  const at = await centre(page, `[id="queue-grip-${second}"]`);
  const finger = await Finger.on(page);
  await finger.down(at.x, at.y);
  await finger.up();

  await expect(page.locator("[data-dragging]")).toHaveCount(0);
  const record = await (await request.get(`/api/tasks/${second}`)).json();
  expect(
    record.log.filter((entry: { type: string }) => entry.type === "queue_move"),
  ).toHaveLength(0);
});

test.describe("a list taller than the screen", () => {
  test.beforeAll(async ({ request }) => {
    // In `low`, so the filler does not sit between the rows the tests above drag.
    await seed(
      request,
      Array.from({ length: 40 }, (_, index) => `${TOKEN}fill ${index}`),
      "low",
    );
  });

  test("a finger swiped on a row, not its grip, scrolls the list and drags nothing", async ({
    page,
  }) => {
    await page.goto(`/app/p/_local/tasks?q=${TOKEN}fill`);
    const rows = page.locator("[data-task]");
    await expect(rows.first()).toBeVisible();
    const rendered = () =>
      rows.evaluateAll((all) => all.map((row) => row.getAttribute("data-task") ?? ""));
    const before = await rendered();
    const startTop = await listScrollTop(page);

    // Start on the row's status cell -- well clear of the grip -- and swipe upward.
    const start = await centre(page, `[data-task="${before[3]}"] [data-field="status"]`);
    const finger = await Finger.on(page);
    await finger.down(start.x, start.y);
    await finger.move(start.x, Math.max(start.y - 500, 80), 20);
    await expect(page.locator("[data-dragging]")).toHaveCount(0);
    await finger.up();

    await expect.poll(() => listScrollTop(page)).toBeGreaterThan(startTop + 100);
    await expect(page.locator("[data-drop-side]")).toHaveCount(0);
    expect(await rendered()).toEqual(before);
  });

  test("a touch drag held at the bottom edge scrolls the list, and stops on release", async ({
    page,
  }) => {
    await page.goto(`/app/p/_local/tasks?q=${TOKEN}fill`);
    const grip = page.locator("[id^=queue-grip-]").first();
    await expect(grip).toBeVisible();
    const startTop = await listScrollTop(page);
    const viewport = page.viewportSize();
    if (!viewport) throw new Error("No viewport.");

    const from = await centre(page, "[id^=queue-grip-] >> nth=0");
    const finger = await Finger.on(page);
    await finger.down(from.x, from.y);
    // Into the bottom edge zone, then held still: a held finger produces no events, so
    // the loop has to keep scrolling from the last reading.
    await finger.move(from.x, viewport.height - 6, 20);
    await expect.poll(() => listScrollTop(page)).toBeGreaterThan(startTop + 100);
    // And the insertion line is still drawn: the finger is on the page's padding below
    // the list, which is where a finger pressed to the bottom of a tablet actually is.
    // Asserted as a row, not a particular one, because the rows are scrolling under it.
    await expect(page.locator("[data-drop-side]")).toHaveCount(1);

    await finger.up();
    const settled = await listScrollTop(page);
    await page.waitForTimeout(300);
    expect(await listScrollTop(page)).toBe(settled);
  });
});
