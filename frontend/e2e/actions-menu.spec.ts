import { expect, test, type Page } from "@playwright/test";

/**
 * task-168: the actions menu, measured in a browser.
 *
 * `src/components/ActionsMenu.test.tsx` owns the behaviour jsdom can see -- what opens,
 * what closes it, where focus goes. Four things it cannot see are here, and each of
 * them is a way this feature fails while every unit test stays green:
 *
 * - **the popup overlays rather than reflows.** jsdom lays nothing out, so `absolute`
 *   is a string to it and a header that grew on open would pass there.
 * - **a thumb can reach every entry.** The `touch-target` convention is a CSS rule, and
 *   the specificity trap documented in `PrimaryNav.tsx` is exactly the kind of thing
 *   that silently defeats it.
 * - **the row still fits on one line.** The trigger costs width at every viewport, so
 *   it moved `NAV_INLINE_MIN_PX`; nothing but a browser can check the new number.
 * - **About reports the running server's version**, compared against what that server
 *   actually answers rather than against a constant this file also holds.
 */

const project = "/app/p/_local";

/** iPhone 14/15 CSS pixels, which is where this app is read over Tailscale. */
const PHONE = { width: 390, height: 844 };
const DESKTOP = { width: 1280, height: 800 };
/** Mirrors `NAV_INLINE_MIN_PX`. */
const NAV_INLINE_MIN_PX = 952;
/** The app's own minimum touch target, from `.touch-target` in `styles.css`. */
const TOUCH_TARGET_PX = 44;

const trigger = (page: Page) => page.getByRole("button", { name: "Actions" });
const menu = (page: Page) => page.getByRole("menu", { name: "Actions" });
const about = (page: Page) => page.getByRole("dialog", { name: "About AgentJobs" });

async function headerHeight(page: Page) {
  return page.evaluate(
    () => document.querySelector("header")?.getBoundingClientRect().height ?? 0,
  );
}

test.describe("the actions menu", () => {
  test.beforeEach(async ({ page }) => {
    await page.setViewportSize(DESKTOP);
    await page.goto(`${project}/tasks`);
    await expect(page.getByRole("navigation", { name: "Primary navigation" })).toBeVisible();
  });

  test("opens a popup holding About, the dispatch pair and the API docs, without moving the header", async ({
    page,
  }) => {
    const before = await headerHeight(page);
    await trigger(page).click();

    await expect(menu(page)).toBeVisible();
    // The whole membership, in order: About, then task-345's three arrivals.
    await expect(menu(page).getByRole("menuitem")).toHaveText([
      "About",
      "Dispatch settings",
      "Playbooks",
      "API Docs",
    ]);
    await expect(menu(page).getByRole("menuitem", { name: "API Docs" })).toHaveAttribute(
      "href",
      "/docs",
    );
    // The whole reason the popup is `absolute`: a bar that grows on open pushes the
    // page down under a header that is pinned, which is the one thing a menu in a
    // pinned header must not do.
    expect(await headerHeight(page)).toBe(before);
  });

  test("is in the top-right corner, opposite the burger it must not be mistaken for", async ({
    page,
  }) => {
    const nav = page.getByRole("navigation", { name: "Primary navigation" });
    const navBox = (await nav.boundingBox())!;
    const triggerBox = (await trigger(page).boundingBox())!;
    // Right of everything else in the bar, and inside it.
    expect(triggerBox.x).toBeGreaterThan(navBox.x + navBox.width / 2);
    expect(triggerBox.x + triggerBox.width).toBeLessThanOrEqual(navBox.x + navBox.width + 1);
  });

  test("closes on Escape and gives focus back to the trigger", async ({ page }) => {
    await trigger(page).click();
    await expect(menu(page)).toBeVisible();

    await page.keyboard.press("Escape");
    await expect(menu(page)).toBeHidden();
    await expect(trigger(page)).toBeFocused();
  });

  test("is reachable and operable from the keyboard alone", async ({ page }) => {
    // Focus the trigger without a pointer, then open it the way a keyboard does.
    await trigger(page).focus();
    await page.keyboard.press("Enter");
    await expect(menu(page)).toBeVisible();
    // Opening puts focus on the first entry, so the next key already moves inside the
    // menu rather than tabbing out of it.
    await expect(menu(page).getByRole("menuitem", { name: "About" })).toBeFocused();
    await page.keyboard.press("ArrowDown");
    await expect(menu(page).getByRole("menuitem", { name: "Dispatch settings" })).toBeFocused();
    await page.keyboard.press("End");
    await expect(menu(page).getByRole("menuitem", { name: "API Docs" })).toBeFocused();
  });

  test("closes when a press lands outside it", async ({ page }) => {
    await trigger(page).click();
    await expect(menu(page)).toBeVisible();
    await page.mouse.click(20, 400);
    await expect(menu(page)).toBeHidden();
  });

  test("closes on a route change", async ({ page }) => {
    await trigger(page).click();
    await expect(menu(page)).toBeVisible();
    await page.getByRole("link", { name: "Dashboard" }).click();
    await expect(menu(page)).toBeHidden();
  });

  test("does not leave the burger panel open underneath itself", async ({ page }) => {
    await page.setViewportSize(PHONE);
    await page.getByRole("button", { name: "Navigation" }).click();
    await expect(page.locator("#primary-nav-destinations")).toBeVisible();

    await trigger(page).click();
    await expect(menu(page)).toBeVisible();
    // Two popups hanging off one bar, one over the other, is the state this avoids.
    await expect(page.locator("#primary-nav-destinations")).toBeHidden();
  });
});

test("About reports the version the server is actually running", async ({ page, request }) => {
  const served = await request.get("/api/version");
  expect(served.ok()).toBeTruthy();
  const version = (await served.json()).version as string;

  await page.setViewportSize(DESKTOP);
  await page.goto(`${project}/tasks`);
  await trigger(page).click();
  await page.getByRole("menuitem", { name: "About" }).click();

  const panel = about(page);
  await expect(panel).toBeVisible();
  // Compared against what this process answers, not against a literal also written
  // here: a constant in the bundle and a constant in the test agree with each other
  // and with nothing else.
  await expect(panel).toContainText(version);
  await expect(panel).toContainText("_local");
  // The menu is replaced rather than stacked on.
  await expect(menu(page)).toBeHidden();
});

test("every entry is tappable at a phone width", async ({ page }) => {
  await page.setViewportSize(PHONE);
  await page.goto(`${project}/tasks`);

  const triggerBox = (await trigger(page).boundingBox())!;
  expect(triggerBox.height).toBeGreaterThanOrEqual(TOUCH_TARGET_PX);
  // The trap `PrimaryNav.tsx` documents is a `.touch-target` element that a breakpoint
  // class fails to hide; the same specificity rule is what would leave this off screen.
  expect(triggerBox.x + triggerBox.width).toBeLessThanOrEqual(PHONE.width);

  await trigger(page).click();
  const entries = menu(page).getByRole("menuitem");
  await expect(entries).toHaveCount(4);
  for (const entry of await entries.all()) {
    const box = (await entry.boundingBox())!;
    expect(box.height).toBeGreaterThanOrEqual(TOUCH_TARGET_PX);
    // Wholly on screen: a popup anchored to the right edge is the one that overflows.
    expect(box.x).toBeGreaterThanOrEqual(0);
    expect(box.x + box.width).toBeLessThanOrEqual(PHONE.width);
  }

  await page.getByRole("menuitem", { name: "About" }).click();
  const panelBox = (await about(page).boundingBox())!;
  expect(panelBox.x).toBeGreaterThanOrEqual(0);
  expect(panelBox.x + panelBox.width).toBeLessThanOrEqual(PHONE.width);
});

test("the bar still fits on one line at the breakpoint the trigger moved", async ({
  page,
  request,
}) => {
  // The attention badge has to be showing, and that is not decoration. It is 34px the
  // row spends only on the screen where work has stopped on you -- which is exactly
  // the screen NAV_INLINE_MIN_PX has to hold for, since it is the one a person is
  // being asked to read. Without it this measured a narrower bar than the constant
  // claims to describe, and would have passed at any breakpoint below the real one.
  const created = await request.post("/api/tasks", {
    data: {
      title: "The bar is measured while something waits on a person",
      summary: "Exists so the attention badge is showing while the row is measured.",
      description: "Handed straight to a human, which is what the badge counts.",
      lifecycle: "ready",
      category: "ux",
      actor: "E2E Human",
    },
  });
  expect(created.ok()).toBeTruthy();
  const fixtureId = (await created.json()).id as string;
  const handed = await request.post(`/api/tasks/${fixtureId}/handoff`, {
    data: {
      actor: "E2E Human",
      ball: "human",
      ball_reason: "decision",
      ball_prompt: "Stand still while the bar is measured.",
    },
  });
  expect(handed.ok()).toBeTruthy();

  try {
    await page.setViewportSize({ width: NAV_INLINE_MIN_PX, height: 800 });
    await page.goto(`${project}/tasks`);
    await expect(page.getByRole("navigation", { name: "Primary navigation" })).toBeVisible();
    await expect(page.getByTestId("attention-badge")).toBeVisible();

    const measured = await page.evaluate(() => {
      const nav = document.querySelector("nav[aria-label='Primary navigation']")!;
      // The switcher pinned to the 224px a long project name reaches, which is the
      // case NAV_INLINE_MIN_PX is defined against rather than the short one this
      // sandbox has. Links forced `nowrap` for the same reason: the constant was
      // measured that way, and a label allowed to wrap hides an overflow as height.
      const style = document.createElement("style");
      style.textContent =
        "nav[aria-label='Primary navigation'] > label { flex: 0 0 224px !important; }" +
        "nav[aria-label='Primary navigation'] a { white-space: nowrap !important; }";
      document.head.appendChild(style);
      return {
        overflow: nav.scrollWidth - nav.clientWidth,
        height: document.querySelector("header")!.getBoundingClientRect().height,
      };
    });

    // `flex-nowrap` does not wrap; it runs off the right edge, so overflow is what a
    // header measured only by its height would miss.
    expect(measured.overflow).toBe(0);
    // One row is `min-h-16` plus a 1px border; anything taller has wrapped.
    expect(measured.height).toBeLessThanOrEqual(72);
    await expect(trigger(page)).toBeVisible();
  } finally {
    // Every spec here shares one server and one project, so a task left blocking
    // becomes the next spec's dashboard headline.
    const closed = await request.post(`/api/tasks/${fixtureId}/close`, {
      data: { actor: "E2E Human", outcome: "cancelled", body: "Measurement fixture." },
    });
    expect(closed.ok()).toBeTruthy();
  }
});

/**
 * ac-2: what task-345 moved in here is still two interactions away, on a phone too.
 *
 * Demonstrated rather than asserted, which is the acceptance criterion's own word: the
 * test presses the trigger and presses the entry, and then checks that the page it
 * asked for is the page it got. Counting the entries in a menu would prove that three
 * links exist, which is not the claim -- the claim is that the machine-wide kill switch
 * is still reachable from wherever you happen to be standing, and that is task-167's
 * constraint rather than a nicety.
 */
test("Dispatch settings, Playbooks and the API docs are two interactions from anywhere", async ({
  page,
}) => {
  // 375px is the narrowest phone this app is read on, and narrower than the 390 the
  // rest of this file uses: the criterion names it, so it is what is measured.
  const NARROW = { width: 375, height: 812 };

  for (const viewport of [DESKTOP, NARROW]) {
    await page.setViewportSize(viewport);
    for (const from of [`${project}`, `${project}/tasks`, `${project}/runs`]) {
      await page.goto(from);
      await expect(page.getByRole("navigation", { name: "Primary navigation" })).toBeVisible();

      // One.
      await trigger(page).click();
      // Two.
      await menu(page).getByRole("menuitem", { name: "Dispatch settings" }).click();
      await expect(page).toHaveURL(/\/p\/_local\/dispatch$/);
      // The switch itself, not merely the address: a route that resolved to an empty
      // page would satisfy a URL assertion and reach nobody.
      await expect(page.getByRole("region", { name: "Dispatch settings" })).toBeVisible();

      await page.goto(from);
      await trigger(page).click();
      await menu(page).getByRole("menuitem", { name: "Playbooks" }).click();
      await expect(page).toHaveURL(/\/p\/_local\/playbooks$/);

      await page.goto(from);
      await trigger(page).click();
      await menu(page).getByRole("menuitem", { name: "API Docs" }).click();
      // FastAPI's own page, so this leaves the app entirely -- which is the reason the
      // entry is an anchor and not a router link, and worth proving once.
      await expect(page).toHaveURL(/\/docs$/);
    }
  }
});
