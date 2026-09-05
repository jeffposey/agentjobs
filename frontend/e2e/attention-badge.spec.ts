import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

/**
 * task-338: the header says work has stopped on you, wherever you are.
 *
 * The reported defect was that only the Dashboard said so, which is the one place you
 * cannot be told something you did not already go looking for. What a jsdom test can
 * prove about the fix is the wiring; the two things it cannot are the two that matter,
 * and both are here:
 *
 * - **that the badge is visible on a phone**, where every destination in this bar is
 *   behind the burger and a badge hung on one of them would render into a hidden
 *   container. jsdom applies no stylesheet, so it would call that a pass.
 * - **that adding it did not break the one-row bar** task-292 measured. It sits
 *   outside the collapsible group, so unlike the Runs badge it costs width at every
 *   viewport -- and only a browser lays out.
 */

const project = "/app/p/_local";

/** iPhone 14/15 CSS pixels, which is where this app is read over Tailscale. */
const PHONE = { width: 390, height: 844 };
/** Exactly `NAV_INLINE_MIN_PX`: the narrowest width that still renders the row inline. */
const NAV_INLINE_MIN_PX = 1140;
const INLINE_MIN = { width: NAV_INLINE_MIN_PX, height: 800 };

/** One row is `min-h-16` plus a 1px bottom border; anything taller has wrapped. */
const ONE_ROW_MAX_PX = 72;

const badge = (page: Page) => page.getByTestId("attention-badge");

/** How many tasks the server says are stopped on a person right now. */
async function blockingCount(request: APIRequestContext): Promise<number> {
  const response = await request.get("/api/projects/_local/attention");
  expect(response.ok()).toBeTruthy();
  return (await response.json()).blocking as number;
}

/**
 * A task handed to a human, returned with the call that puts the corpus back.
 *
 * Every spec in this directory shares one server and one project, so a task left
 * blocking would become every later spec's dashboard headline.
 */
async function blockedTask(request: APIRequestContext, title: string) {
  const created = await request.post("/api/tasks", {
    data: {
      title,
      summary: "Exists so the header has something to raise a badge about.",
      description: "Handed straight to a human, which is what the badge counts.",
      lifecycle: "ready",
      category: "ux",
      actor: "E2E Human",
    },
  });
  expect(created.ok()).toBeTruthy();
  const id = (await created.json()).id as string;

  const handed = await request.post(`/api/tasks/${id}/handoff`, {
    data: {
      actor: "E2E Human",
      ball: "human",
      ball_reason: "decision",
      ball_prompt: "Decide whether the badge should be red.",
    },
  });
  expect(handed.ok()).toBeTruthy();

  return {
    id,
    async release() {
      const closed = await request.post(`/api/tasks/${id}/close`, {
        data: { actor: "E2E Human", outcome: "cancelled", body: "Badge fixture; nothing to do." },
      });
      expect(closed.ok()).toBeTruthy();
    },
  };
}

/**
 * The primary nav's own rectangle and whether its contents fit inside it.
 *
 * `pinSwitcher` reproduces the case `NAV_INLINE_MIN_PX` is defined against rather than
 * the one this sandbox happens to have: the project switcher at the 224px (`max-w-56`)
 * it reaches for a long project name. Without it the switcher here is far narrower and
 * the measurement is vacuous -- it would pass at any breakpoint at all. Below the
 * breakpoint it is not applied, because `min-w-0` squeezing the switcher is the
 * intended behaviour on a phone.
 */
async function navBox(page: Page, { pinSwitcher = false } = {}) {
  return page.evaluate((pin: boolean) => {
    const nav = document.querySelector("nav[aria-label='Primary navigation']");
    const header = document.querySelector("header");
    if (!nav || !header) return null;
    if (pin) {
      const style = document.createElement("style");
      style.textContent =
        "nav[aria-label='Primary navigation'] > label { flex: 0 0 224px !important; }";
      document.head.appendChild(style);
    }
    return {
      headerHeight: header.getBoundingClientRect().height,
      // A `flex-nowrap` row does not wrap; it runs off the right edge, so overflow is
      // what a header measured only by its height would miss.
      overflow: nav.scrollWidth - nav.clientWidth,
    };
  }, pinSwitcher);
}

test("the badge appears on a surface that is not the Dashboard, without a reload", async ({
  page,
  request,
}) => {
  // Two revision polls have to land inside this test and they are 15 seconds apart,
  // so Playwright's 30-second default cannot hold it. The cap is on the *test*, not on
  // the assertion: a generous `expect` timeout inside a 30-second test still dies at
  // 30 seconds, reported as the assertion failing rather than as the budget running
  // out, which is a convincing-looking way to be told nothing at all.
  test.setTimeout(120_000);

  const before = await blockingCount(request);

  await page.goto(`${project}/tasks`);
  await expect(page.getByRole("navigation", { name: "Primary navigation" })).toBeVisible();

  const fixture = await blockedTask(request, "Something is waiting on you");
  try {
    // Nothing here reloads the page. If this passes, the badge is refreshed by the
    // project revision the way every other task-derived query is -- which is the
    // difference between a badge and a screenshot of one.
    await expect(badge(page)).toHaveText(String(before + 1), { timeout: 30_000 });
    await expect(badge(page)).toHaveAttribute(
      "aria-label",
      new RegExp(`^${before + 1} tasks? (is|are) waiting on you$`),
    );
  } finally {
    await fixture.release();
  }

  // And it goes away again when the work does. Absence, not a zero: this one is an
  // alarm rather than a readout, unlike the live-run count beside it.
  if (before === 0) {
    await expect(badge(page)).toHaveCount(0, { timeout: 30_000 });
  }
});

test("the badge leads to the tasks it is counting", async ({ page, request }) => {
  const fixture = await blockedTask(request, "Follow the badge to me");
  try {
    await page.goto(`${project}/tasks`);
    await badge(page).click();

    await expect(page).toHaveURL(new RegExp(`${project}$`));
    // The Dashboard's own panel, computed from the same predicate as the badge.
    await expect(page.getByTestId("next-action")).toContainText("Work has stopped on these");
    await expect(page.getByTestId("next-action")).toContainText("Follow the badge to me");
  } finally {
    await fixture.release();
  }
});

for (const [name, viewport] of [
  ["a phone", PHONE],
  ["the narrowest inline width", INLINE_MIN],
] as const) {
  test(`the badge is visible on ${name}, and the bar is still one row`, async ({
    page,
    request,
  }) => {
    const fixture = await blockedTask(request, "Visible at every width");
    try {
      await page.setViewportSize(viewport);
      await page.goto(`${project}/tasks`);

      // Visible with nothing opened. Below the breakpoint the destinations are behind
      // the burger, so this is the assertion the whole placement decision exists for.
      await expect(badge(page)).toBeVisible();
      if (viewport.width < NAV_INLINE_MIN_PX) {
        const nav = page.getByRole("navigation", { name: "Primary navigation" });
        await expect(nav.getByText("Tasks", { exact: true })).toBeHidden();
      }

      const box = await navBox(page, { pinSwitcher: viewport.width >= NAV_INLINE_MIN_PX });
      expect(box, "the page renders the primary nav").not.toBeNull();
      expect(
        box!.headerHeight,
        `${name} renders the bar as one row with the badge present`,
      ).toBeLessThanOrEqual(ONE_ROW_MAX_PX);
      expect(box!.overflow, `${name} fits the bar's contents inside it`).toBeLessThanOrEqual(0);
    } finally {
      await fixture.release();
    }
  });
}
