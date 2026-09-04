import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

/**
 * task-292: the header stays at the top of the viewport, and it is one row while it
 * does it.
 *
 * Everything here is asserted on a measured rectangle in a real browser. A jsdom test
 * cannot see any of it -- jsdom does not lay out, so `getBoundingClientRect()` is
 * always zero and `position: sticky` has no meaning -- and a test that asserted
 * `class="sticky"` instead would pass against a header nested inside an ancestor with
 * `overflow: hidden`, where sticky silently does nothing. ENGINEERING.md, Verification.
 */

const DESKTOP = { width: 1280, height: 800 };
/** iPhone 14/15 CSS pixels, which is where this app is read over Tailscale. */
const PHONE = { width: 390, height: 844 };

/** One row is `min-h-16` plus a 1px bottom border; anything taller has wrapped. */
const ONE_ROW_MAX_PX = 72;

const DESTINATIONS = [
  "Dashboard",
  "Tasks",
  "Create",
  "Dispatch",
  "Playbooks",
  "Runs",
  "API Docs",
];

/** Mirrors `NAV_INLINE_MIN_PX`; below it the destinations are behind the burger. */
const NAV_INLINE_MIN_PX = 1100;

/**
 * One record, long enough that every viewport under test has somewhere to scroll to,
 * created once and reused by every test in this file.
 *
 * Deliberately not one per test. Every spec in this directory shares a single server
 * and a single project, and `queue-order.spec.ts` drives a raw-mouse drag between two
 * rows that both have to be inside one viewport at the same time -- so each task a
 * spec adds pushes that pair further apart. Four fixtures from this file were enough
 * to put them 1300px apart in a 720px window and turn that spec red. Measured, after
 * a wrong first guess that the pinned header was occluding the press.
 */
let longTaskId: string | null = null;

async function longTask(request: APIRequestContext) {
  if (longTaskId) return longTaskId;
  const paragraph =
    "A paragraph of the working specification, repeated so the rendered page is " +
    "taller than a phone screen and taller than a desktop one. ";
  const response = await request.post("/api/tasks", {
    data: {
      title: "A task with a specification long enough to scroll",
      summary: "Exists so the pinned header has something to stay pinned over.",
      description: paragraph.repeat(120),
      lifecycle: "ready",
      category: "ux",
      actor: "E2E Human",
    },
  });
  expect(response.ok()).toBeTruthy();
  longTaskId = (await response.json()).id as string;

  // Closed immediately, and this is the point of the fixture rather than an
  // afterthought: the task list defaults to open work, so a closed record still has
  // the long detail page these tests need while adding no row to the list every other
  // spec shares. Leaving it open put one extra row between the two tasks
  // `queue-order.spec.ts` drags between, which was enough to move the target 328px
  // below the fold and turn that spec red -- measured, at 1280x720, after two wrong
  // guesses about the pinned header.
  const closed = await request.post(`/api/tasks/${longTaskId}/close`, {
    data: { actor: "E2E Human", outcome: "completed", body: "Fixture; nothing to review." },
  });
  expect(closed.ok()).toBeTruthy();
  return longTaskId;
}

/** Scroll the document to its end, refusing to pass on a page that never scrolled. */
async function scrollToEnd(page: Page) {
  const scrolled = await page.evaluate(() => {
    window.scrollTo(0, document.documentElement.scrollHeight);
    return window.scrollY;
  });
  // Without this the whole test is vacuous: a header is trivially at the top of a
  // page that has no scroll.
  expect(scrolled, "the page under test must actually scroll").toBeGreaterThan(0);
}

/**
 * The header's rectangle in viewport coordinates, read from the browser itself.
 *
 * `getBoundingClientRect()` rather than Playwright's `boundingBox()`, because this
 * test turns entirely on the difference between viewport-relative and page-relative
 * coordinates and only one of the two is unambiguously the former.
 */
async function headerBox(page: Page) {
  const box = await page.evaluate(() => {
    // First in document order is the app's own banner; pages may carry a nested
    // <header> of their own further down.
    const header = document.querySelector("header");
    if (!header) return null;
    const rect = header.getBoundingClientRect();
    return { x: rect.x, y: rect.y, width: rect.width, height: rect.height };
  });
  expect(box, "the page renders a header").not.toBeNull();
  return box as { x: number; y: number; width: number; height: number };
}

for (const [name, viewport] of [
  ["a desktop window", DESKTOP],
  ["a phone", PHONE],
] as const) {
  test(`the header stays at the top of ${name} on every surface`, async ({ page, request }) => {
    const taskId = await longTask(request);
    await page.setViewportSize(viewport);

    const surfaces = [
      `/app/p/_local/tasks/${taskId}`,
      "/app/p/_local",
      "/app/p/_local/tasks",
      "/app/p/_local/tasks/new",
      "/app/p/_local/dispatch",
      "/app/p/_local/playbooks",
      "/app/p/_local/runs",
    ];

    for (const surface of surfaces) {
      await page.goto(surface);
      // `banner`, not `header`: the Create surface has a second, nested <header> of
      // its own, and a bare tag selector matches both.
      await expect(page.getByRole("banner")).toBeVisible();

      const atRest = await headerBox(page);
      expect(atRest.y, `${surface} starts with the header at the top`).toBe(0);
      // The pin is only an improvement if the bar is one row. Pinning the wrapped
      // three-row bar would spend 19% of a phone screen permanently on navigation.
      expect(atRest.height, `${surface} renders the bar as one row`).toBeLessThanOrEqual(
        ONE_ROW_MAX_PX,
      );

      const canScroll = await page.evaluate(
        () => document.documentElement.scrollHeight > window.innerHeight,
      );
      if (!canScroll) continue;

      await scrollToEnd(page);
      const afterScroll = await headerBox(page);
      expect(afterScroll.y, `${surface} keeps the header at the top after scrolling`).toBe(0);
    }
  });
}

test("the Tasks link is reachable from the bottom of a long page, at both viewports", async ({
  page,
  request,
}) => {
  const taskId = await longTask(request);

  for (const viewport of [DESKTOP, PHONE]) {
    await page.setViewportSize(viewport);
    await page.goto(`/app/p/_local/tasks/${taskId}`);
    await scrollToEnd(page);

    if (viewport.width < NAV_INLINE_MIN_PX) {
      // Below the breakpoint the destinations live behind the burger, which is itself
      // in the pinned bar and therefore on screen.
      await page.getByRole("button", { name: "Navigation" }).click();
    }
    await page.getByRole("link", { name: "Tasks", exact: true }).click();
    await expect(page).toHaveURL(/\/p\/_local\/tasks(\?|$)/);
  }
});

test("above the breakpoint every destination is inline and there is no burger", async ({
  page,
}) => {
  await page.setViewportSize(DESKTOP);
  await page.goto("/app/p/_local");

  await expect(page.getByRole("button", { name: "Navigation" })).toBeHidden();
  const nav = page.getByRole("navigation", { name: "Primary navigation" });
  for (const label of DESTINATIONS) {
    await expect(nav.getByText(label, { exact: true })).toBeVisible();
  }
});

test("below the breakpoint the destinations are behind the burger, and all of them are there", async ({
  page,
}) => {
  await page.setViewportSize(PHONE);
  await page.goto("/app/p/_local");

  const burger = page.getByRole("button", { name: "Navigation" });
  await expect(burger).toBeVisible();
  const nav = page.getByRole("navigation", { name: "Primary navigation" });
  await expect(nav.getByText("Playbooks", { exact: true })).toBeHidden();

  await burger.click();
  const panel = page.locator("#primary-nav-destinations");
  await expect(panel).toBeVisible();
  for (const label of DESTINATIONS) {
    await expect(panel.getByText(label, { exact: true })).toBeVisible();
  }

  // It overlays the page rather than pushing it down: the bar is the same height
  // open as closed, which is what stops opening the menu reflowing what you were
  // reading underneath it.
  const box = await headerBox(page);
  expect(box.height).toBeLessThanOrEqual(ONE_ROW_MAX_PX);
});

test("the Report Issue modal still covers the pinned header, and its button still works", async ({
  page,
}) => {
  for (const viewport of [DESKTOP, PHONE]) {
    await page.setViewportSize(viewport);
    await page.goto("/app/p/_local");

    // The floating button is `z-40` and the header is `z-30`; the button opening at
    // all is the evidence it is still hit-testable above whatever is beneath it.
    await page.getByRole("button", { name: "Report issue" }).click();
    const dialog = page.getByRole("dialog", { name: "Report an issue" });
    await expect(dialog).toBeVisible();

    // The header is `z-30` and the overlay `z-50`, so the topmost element over the
    // bar must belong to the dialog. Asked of the browser's own hit test rather than
    // of the class names, because a stacking context anywhere between them would
    // change the answer without changing any z-index.
    const overHeader = await page.evaluate(() => {
      const element = document.elementFromPoint(Math.round(window.innerWidth / 2), 8);
      // The dialog itself is a card in the middle of a full-screen scrim, so the
      // element over the header is the scrim rather than the dialog. Walking up to
      // the nearest fixed ancestor names it without depending on a class name.
      let overlay: Element | null = document.querySelector('[role="dialog"]');
      while (overlay && getComputedStyle(overlay).position !== "fixed") {
        overlay = overlay.parentElement;
      }
      return {
        insideHeader: Boolean(element?.closest("header")),
        insideOverlay: Boolean(overlay && element && overlay.contains(element)),
      };
    });
    expect(overHeader.insideOverlay, "the reporter's overlay covers the top of the viewport").toBe(
      true,
    );
    expect(overHeader.insideHeader, "the header does not punch through the dialog").toBe(false);

    await page.getByRole("button", { name: "Cancel" }).click();
    await expect(dialog).toBeHidden();
  }
});

test("the geometry assertion has teeth: unpinning the header makes it fail", async ({
  page,
  request,
}) => {
  const taskId = await longTask(request);
  await page.setViewportSize(PHONE);
  await page.goto(`/app/p/_local/tasks/${taskId}`);

  // A negative control, in the test rather than in a reviewer's head. ac-5 asks that
  // the assertion fail when `sticky`/`top-0` is removed; this removes them and shows
  // that it does, so nobody has to take it on trust that the check would notice.
  await page.evaluate(() => {
    document.querySelector("header")?.classList.remove("sticky", "top-0");
  });
  await scrollToEnd(page);

  const box = await headerBox(page);
  expect(box.y, "an unpinned header leaves the viewport when the page scrolls").toBeLessThan(0);
});

test("what the browser scrolls to lands below the bar, not underneath it", async ({
  page,
  request,
}) => {
  // The pin's one side effect on everything else in the app. `scrollIntoView`, a
  // fragment link, and the scroll a browser performs when Tab reaches an off-screen
  // control all stop at the top of the scrollport -- which is now behind 65px of
  // opaque header. `scroll-padding-top` in styles.css moves that stop line down, and
  // this is what proves it did; without it this assertion fails by exactly the
  // header's height.
  const taskId = await longTask(request);
  await page.setViewportSize(PHONE);
  await page.goto(`/app/p/_local/tasks/${taskId}`);

  // Something far enough down the page that reaching it requires a scroll.
  const target = page.getByRole("region", { name: "Task log" });
  await expect(target).toBeAttached();
  await target.evaluate((element) => element.scrollIntoView());

  const headerHeight = (await headerBox(page)).height;
  const top = await target.evaluate((element) => element.getBoundingClientRect().top);
  expect(top, "what the browser scrolled to is not hidden behind the pinned bar").toBeGreaterThanOrEqual(
    headerHeight,
  );
});
