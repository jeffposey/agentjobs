import { expect, test, type Page, type Route } from "@playwright/test";

/**
 * task-294: the Dashboard is one screen, and the document never scrolls on it.
 *
 * Every assertion here is a measured rectangle in a real browser. A class name is not
 * evidence: `h-dvh` inside an ancestor that is itself `min-h-screen` produces a frame
 * one URL-bar taller than the viewport and scrolls anyway, and `overflow-hidden` on a
 * parent whose child has no `min-h-0` clips instead of fitting. Only the geometry can
 * tell those apart -- ENGINEERING.md, Verification.
 *
 * **The content is amplified, not fabricated.** The suite seeds two real tasks, and
 * each test then multiplies the server's own `active_tasks` list into forty entries
 * before the page sees it. The shape under test is therefore the real response and the
 * volume is the worst case a real project reaches -- `active_tasks` is uncapped by the
 * server, so a real backlog genuinely puts forty cards on this page. Before this task
 * that made the Dashboard 5681px tall on a desktop and 8566px on a phone.
 *
 * The two seeds are closed again in `afterAll`. Every spec here shares one project and
 * one server, and `queue-order.spec.ts` drags between two rows that must fit in one
 * viewport together, so an open row left behind by this file is a red spec somewhere
 * else -- the fixture comment in `pinned-header.spec.ts` records that happening.
 */

const DASHBOARD = "/app/p/_local";

/**
 * The viewports the Dashboard must fit, named rather than sampled.
 *
 * Portrait phone is where this app is read over Tailscale; the desktop and laptop are
 * the windows it is developed in. Landscape phone is the one that decides the design:
 * 390px of height, carrying the same header and the same board as the 844px portrait.
 */
const VIEWPORTS = {
  phone: { width: 390, height: 844 },
  desktop: { width: 1280, height: 800 },
  laptop: { width: 1440, height: 900 },
  "phone in landscape": { width: 844, height: 390 },
} as const;

/**
 * The ceilings the frame is measured at (ac-7).
 *
 * One, three and six, because the board draws `min(max_concurrent_runs, 6)` cells and
 * task-344 turns that number into one a person types. One is a single full-width
 * panel, three is the common workstation, six is the board's own cap -- and six on a
 * phone is the tallest the board can ever be, since it is one column there.
 */
const CEILINGS = [1, 3, 6] as const;

/**
 * Where the glance -- the board and the one call to action -- is whole with no scroll.
 *
 * The document-never-scrolls assertion below holds at every viewport and every ceiling.
 * This is the stronger claim, and it is deliberately not made for every combination.
 * Two are declared out of it and degrade to a scroll inside the glance's own region,
 * which is a legitimate fallback and is not the same as being clipped:
 *
 * - **A phone in landscape**, at 390px of height. There are 293px inside the frame
 *   there and a three-slot board is 446px; no arrangement of a board fits.
 * - **A six-slot ceiling on a portrait phone**, where the board is one column of six
 *   cards, 1096px of it. Two columns at 390px wide would be 175px per cell, which is
 *   not enough for the summary a free cell exists to show (task-092).
 *
 * The test after this one is what proves those two are short rather than truncated.
 */
const GLANCE_MUST_BE_WHOLE: ReadonlyArray<[keyof typeof VIEWPORTS, number]> = [
  ["phone", 1],
  ["phone", 3],
  ["desktop", 1],
  ["desktop", 3],
  ["desktop", 6],
  ["laptop", 1],
  ["laptop", 3],
  ["laptop", 6],
];

/** How many active tasks the amplified response carries. */
const CROWD = 40;

const seeded: string[] = [];

test.beforeAll(async ({ request }) => {
  // Two, not one: `active_tasks` is amplified from whatever the server sends, and one
  // seed would make every amplified card identical, which is a layout this page is
  // never asked to draw. Both are `ready`, so they are open work on the board's queue
  // and in the active list at the same time -- which is the state being measured.
  for (const [index, title] of [
    "A task with a title long enough to test how a card truncates it",
    "A short one",
  ].entries()) {
    const response = await request.post("/api/tasks", {
      data: {
        title,
        summary:
          "Seeded by dashboard-one-screen.spec.ts so the Dashboard has active work, a " +
          "queue and a log to render. Closed again when this file finishes.",
        description: `Fixture ${index + 1} for task-294's geometry measurements.`,
        lifecycle: "ready",
        category: "ux",
        actor: "E2E Human",
      },
    });
    expect(response.ok(), await response.text()).toBeTruthy();
    seeded.push((await response.json()).id as string);
  }
});

test.afterAll(async ({ request }) => {
  for (const id of seeded) {
    await request.post(`/api/tasks/${id}/close`, {
      data: { actor: "E2E Human", outcome: "completed", body: "Fixture; nothing to review." },
    });
  }
});

/** Replace a route's body while keeping its status and content type. */
async function fulfilJson(route: Route, body: unknown) {
  await route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

/** Amplify the real dashboard response so the unbounded lists are at their worst. */
async function withCrowdedDashboard(page: Page) {
  await page.route("**/api/projects/*/dashboard*", async (route: Route) => {
    const response = await route.fetch();
    const body = (await response.json()) as { active_tasks: unknown[] };
    const seed = body.active_tasks;
    expect(seed.length, "the seeded tasks reached the dashboard response").toBeGreaterThan(0);
    const grown: unknown[] = [];
    for (let index = 0; index < CROWD; index += 1) {
      const clone = JSON.parse(JSON.stringify(seed[index % seed.length])) as {
        id: string;
        title: string;
      };
      clone.id = `${clone.id}-crowd-${index}`;
      clone.title = `${clone.title} (${index + 1})`;
      grown.push(clone);
    }
    body.active_tasks = grown;
    await fulfilJson(route, body);
  });
}

/** Pin the machine's answer so the board draws a known number of cells. */
async function withCeiling(page: Page, ceiling: number) {
  await page.route("**/api/runs/live", async (route: Route) => {
    const response = await route.fetch();
    const body = (await response.json()) as Record<string, unknown>;
    body.dispatch_configured = true;
    body.max_concurrent_runs = ceiling;
    await fulfilJson(route, body);
  });
}

/**
 * Open the Dashboard and wait for the board, which is the last thing to arrive.
 *
 * The board renders nothing until the machine-wide query answers, so measuring before
 * it lands would measure a page one section shorter than the real one.
 */
async function openDashboard(page: Page) {
  await page.goto(DASHBOARD);
  await expect(page.getByTestId("slot-board")).toBeVisible();
}

/** What the browser says about the document's own scroll. */
async function documentScroll(page: Page) {
  return page.evaluate(() => ({
    scrollHeight: document.documentElement.scrollHeight,
    innerHeight: window.innerHeight,
  }));
}

/** What the browser says about one of the Dashboard's two scroll regions. */
async function regionScroll(page: Page, testId: string) {
  const measured = await page.evaluate((id) => {
    const region = document.querySelector(`[data-testid="${id}"]`);
    if (!region) return null;
    return { scrollHeight: region.scrollHeight, clientHeight: region.clientHeight };
  }, testId);
  expect(measured, `${testId} is in the DOM`).not.toBeNull();
  return measured as { scrollHeight: number; clientHeight: number };
}

for (const [name, viewport] of Object.entries(VIEWPORTS)) {
  for (const ceiling of CEILINGS) {
    test(`the Dashboard document does not scroll on a ${name} with ${ceiling} slots`, async ({
      page,
    }) => {
      await withCeiling(page, ceiling);
      await withCrowdedDashboard(page);
      await page.setViewportSize(viewport);
      await openDashboard(page);

      const scroll = await documentScroll(page);
      expect(
        scroll.scrollHeight,
        `${name} at ${ceiling} slots: the document is ${scroll.scrollHeight}px in a ` +
          `${scroll.innerHeight}px viewport`,
      ).toBeLessThanOrEqual(scroll.innerHeight);
    });

    test(`nothing on the Dashboard is clipped on a ${name} with ${ceiling} slots`, async ({
      page,
    }) => {
      // A document that does not scroll is worth nothing if the page bought that by
      // being cut off. Each region must be able to scroll to its own last section's
      // bottom edge -- which is the difference between "short" and "truncated", and the
      // property that makes a scrolling fallback legitimate.
      await withCeiling(page, ceiling);
      await withCrowdedDashboard(page);
      await page.setViewportSize(viewport);
      await openDashboard(page);

      for (const id of ["dashboard-glance", "dashboard-tail"]) {
        const reached = await page.evaluate((testId) => {
          const region = document.querySelector(`[data-testid="${testId}"]`);
          if (!region) return null;
          region.scrollTop = region.scrollHeight;
          const last = region.lastElementChild;
          if (!last) return null;
          return {
            bottom: last.getBoundingClientRect().bottom,
            frame: region.getBoundingClientRect().bottom,
          };
        }, id);
        expect(reached, `${id} has sections in it`).not.toBeNull();
        expect(
          reached!.bottom,
          `${name} at ${ceiling} slots: ${id} can be scrolled to its last section`,
          // One pixel of tolerance for sub-pixel layout; a clipped region is short by
          // hundreds, never by one.
        ).toBeLessThanOrEqual(reached!.frame + 1);
      }
    });
  }
}

for (const [name, ceiling] of GLANCE_MUST_BE_WHOLE) {
  test(`the board and the call to action are whole on a ${name} with ${ceiling} slots`, async ({
    page,
  }) => {
    await withCeiling(page, ceiling);
    await withCrowdedDashboard(page);
    await page.setViewportSize(VIEWPORTS[name]);
    await openDashboard(page);

    const glance = await regionScroll(page, "dashboard-glance");
    expect(
      glance.scrollHeight,
      `${name} at ${ceiling} slots: the glance holds ${glance.scrollHeight}px of content ` +
        `in ${glance.clientHeight}px`,
    ).toBeLessThanOrEqual(glance.clientHeight);
  });
}

test("the tail is a remainder, and it never disappears", async ({ page }) => {
  // A phone at six slots is the case the floor exists for: the board wants 1096px of a
  // 747px frame, so without `TAIL_MIN` the tail would resolve to nothing and take both
  // its sections with it -- unreachable, rather than short.
  await withCeiling(page, 6);
  await withCrowdedDashboard(page);
  await page.setViewportSize(VIEWPORTS.phone);
  await openDashboard(page);

  const tail = await regionScroll(page, "dashboard-tail");
  // `TAIL_MIN`, which is 6rem.
  expect(tail.clientHeight, "the tail keeps a floor of its own").toBeGreaterThanOrEqual(96);
  await expect(page.getByRole("heading", { name: "Active tasks" })).toBeVisible();
});

test("the assertion has teeth: unframing the shell makes the Dashboard scroll again", async ({
  page,
}) => {
  // A negative control, in the test rather than in a reviewer's head. Without it a
  // green run above is equally consistent with a Dashboard that has nothing on it.
  await withCeiling(page, 3);
  await withCrowdedDashboard(page);
  await page.setViewportSize(VIEWPORTS.phone);
  await openDashboard(page);

  await page.evaluate(() => {
    // Put the page back the way it was before task-294: a document-height shell, and
    // regions with no scrollport of their own to hold the overflow.
    document.querySelector("header")?.parentElement?.classList.remove("h-dvh", "overflow-hidden");
    for (const id of ["dashboard-glance", "dashboard-tail"]) {
      document
        .querySelector(`[data-testid="${id}"]`)
        ?.classList.remove("overflow-y-auto", "min-h-0");
    }
  });

  const scroll = await documentScroll(page);
  expect(
    scroll.scrollHeight,
    "an unframed shell scrolls the document again, so the frame is what is holding it",
  ).toBeGreaterThan(scroll.innerHeight);
});

test("the count-tile strip is gone, and everything else is still on the page", async ({
  page,
}) => {
  await withCeiling(page, 3);
  await withCrowdedDashboard(page);
  await page.setViewportSize(VIEWPORTS.desktop);
  await openDashboard(page);

  // ac-4: the five-tile strip no longer renders.
  await expect(page.getByRole("region", { name: "Task statistics" })).toHaveCount(0);

  // ac-6: what stayed. The board, both tail sections, and the link that makes the
  // shortened Active tasks list a sample rather than a truncation.
  await expect(page.getByTestId("slot-board")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Active tasks" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Recent updates" })).toBeVisible();
  await expect(page.getByRole("link", { name: `View all ${CROWD} →` })).toHaveAttribute(
    "href",
    "/app/p/_local/tasks",
  );
});

test("the frame is the Dashboard's alone: every other surface still scrolls its document", async ({
  page,
  request,
}) => {
  // The decision this task took was to frame one surface, not the shell, so that
  // `dragAutoScroll.ts` keeps the window as its scroller on the Tasks page. That is a
  // property of the running app, and this is what holds it.
  const paragraph =
    "A paragraph of the working specification, repeated until the detail page is " +
    "taller than the window it is read in. ";
  const created = await request.post("/api/tasks", {
    data: {
      title: "A task long enough to prove the other surfaces still scroll",
      summary: "Closed immediately; it exists for one measurement.",
      description: paragraph.repeat(80),
      lifecycle: "ready",
      category: "ux",
      actor: "E2E Human",
    },
  });
  expect(created.ok()).toBeTruthy();
  const taskId = (await created.json()).id as string;
  await request.post(`/api/tasks/${taskId}/close`, {
    data: { actor: "E2E Human", outcome: "completed", body: "Fixture; nothing to review." },
  });

  await page.setViewportSize(VIEWPORTS.phone);
  await page.goto(`${DASHBOARD}/tasks/${taskId}`);
  await expect(page.getByRole("banner")).toBeVisible();

  const scroll = await documentScroll(page);
  expect(
    scroll.scrollHeight,
    "the task detail page is still the document's own scroll, not a framed region",
  ).toBeGreaterThan(scroll.innerHeight);

  // And the window is still what scrolls it -- which is the property `window.scrollBy`
  // in dragAutoScroll.ts depends on.
  const scrolled = await page.evaluate(() => {
    window.scrollBy(0, 300);
    return window.scrollY;
  });
  expect(scrolled, "window.scrollBy still moves this surface").toBeGreaterThan(0);
});
