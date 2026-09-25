import { expect, test, type APIRequestContext, type Page } from "./fixtures";

/**
 * task-573: the header's emergency stop, measured and pressed in a browser.
 *
 * `src/components/EmergencyStop.test.tsx` owns what jsdom can see: the confirm step, the
 * result list, the Resume path. What only a browser can say is here: the control sits
 * immediately left of the capture trigger, on every page, and the row still fits in
 * *both* of its states -- the stopped pill is wider than the stop glyph, and the stopped
 * screen is exactly the one a person is being asked to read.
 *
 * Pressing it writes this worker's own sentinel. Each worker has its own AgentJobs
 * home, so no neighbour sees it, and every test here lifts it again on the way out.
 */

const project = "/app/p/_local";
const PHONE = { width: 375, height: 812 };
const DESKTOP = { width: 1280, height: 800 };
/** Mirrors `NAV_INLINE_MIN_PX`, as actions-menu.spec.ts does. */
const NAV_INLINE_MIN_PX = 796;

const stop = (page: Page) => page.getByTestId("emergency-stop");
const plus = (page: Page) => page.getByRole("button", { name: /^New task or issue/ });

async function lift(request: APIRequestContext) {
  const resumed = await request.post("/api/runs/emergency-stop/resume");
  expect(resumed.ok()).toBeTruthy();
}

/** The stop's right edge against the plus's left edge, on the same row. */
async function expectLeftOfPlus(page: Page) {
  const stopBox = (await stop(page).boundingBox())!;
  const plusBox = (await plus(page).boundingBox())!;
  expect(stopBox.x + stopBox.width).toBeLessThanOrEqual(plusBox.x);
  // Nothing sits between them: the gap is the actions group's `gap-1`, give or take.
  expect(plusBox.x - (stopBox.x + stopBox.width)).toBeLessThanOrEqual(8);
  expect(Math.abs(stopBox.y + stopBox.height / 2 - (plusBox.y + plusBox.height / 2))).toBeLessThan(
    2,
  );
}

async function navOverflow(page: Page, pin: boolean): Promise<number> {
  return page.evaluate((pinIt) => {
    const nav = document.querySelector("nav[aria-label='Primary navigation']")!;
    if (!pinIt) return nav.scrollWidth - nav.clientWidth;
    // Pinned exactly as actions-menu.spec.ts pins it, because NAV_INLINE_MIN_PX is
    // defined against the long project name, not this sandbox's short one. Not on a
    // phone, where the switcher is meant to give up its width.
    const style = document.createElement("style");
    style.textContent =
      "nav[aria-label='Primary navigation'] > label { flex: 0 0 224px !important; }" +
      "nav[aria-label='Primary navigation'] a { white-space: nowrap !important; }";
    document.head.appendChild(style);
    return nav.scrollWidth - nav.clientWidth;
  }, pin);
}

test.afterEach(async ({ request }) => {
  await lift(request);
});

for (const [label, size] of [
  ["desktop", DESKTOP],
  ["a 375px phone", PHONE],
] as const) {
  test(`sits immediately left of the plus on ${label}, on every page`, async ({ page }) => {
    await page.setViewportSize(size);
    for (const path of [project, `${project}/tasks`, "/app/not-found"]) {
      await page.goto(path);
      await expect(stop(page)).toBeVisible();
      await expect(stop(page)).toHaveAttribute("data-stopped", "false");
      await expectLeftOfPlus(page);
    }
  });
}

test("the row fits at the breakpoint and on a phone, stopped or not", async ({
  page,
  request,
}) => {
  for (const pressed of [false, true]) {
    if (pressed) {
      const response = await request.post("/api/runs/emergency-stop");
      expect(response.ok()).toBeTruthy();
    }
    for (const width of [NAV_INLINE_MIN_PX, PHONE.width]) {
      await page.setViewportSize({ width, height: 800 });
      await page.goto(`${project}/tasks`);
      await expect(stop(page)).toHaveAttribute("data-stopped", String(pressed));
      expect(await navOverflow(page, width >= NAV_INLINE_MIN_PX), `${width}px, stopped=${pressed}`).toBe(0);
      await expect(page.getByRole("button", { name: "Actions" })).toBeInViewport();
    }
  }
});

test("pressed on a phone: one confirm, the results, then Stopped on every page until Resume", async ({
  page,
}) => {
  await page.setViewportSize(PHONE);
  await page.goto(`${project}/tasks`);

  await stop(page).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByRole("heading", { name: "Stop everything?" })).toBeVisible();
  await dialog.getByRole("button", { name: "Stop everything" }).click();
  await expect(dialog.getByRole("heading", { name: "Dispatch stopped" })).toBeVisible();
  await expect(dialog).toContainText("from the web UI");
  await dialog.getByRole("button", { name: "Close" }).click();

  for (const path of [project, `${project}/tasks`, "/app/not-found"]) {
    await page.goto(path);
    await expect(stop(page)).toHaveAttribute("data-stopped", "true");
    await expect(page.getByTestId("stopped-banner")).toBeVisible();
    await expectLeftOfPlus(page);
  }

  // And a dispatch really is refused while it stands -- read from the server, not the pill.
  const state = await page.request.get(`/api/projects/_local/dispatch`);
  expect((await state.json()).sentinel_active).toBe(true);

  await page.getByRole("button", { name: "Resume…" }).click();
  await page.getByRole("button", { name: "Resume dispatch" }).click();
  await expect(stop(page)).toHaveAttribute("data-stopped", "false");
  await expect(page.getByTestId("stopped-banner")).toHaveCount(0);
  const after = await page.request.get(`/api/projects/_local/dispatch`);
  expect((await after.json()).sentinel_active).toBe(false);
});
