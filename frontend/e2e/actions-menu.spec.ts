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
const NAV_INLINE_MIN_PX = 1256;
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

  test("opens a popup holding About and the API docs, without moving the header", async ({
    page,
  }) => {
    const before = await headerHeight(page);
    await trigger(page).click();

    await expect(menu(page)).toBeVisible();
    await expect(menu(page).getByRole("menuitem", { name: "About" })).toBeVisible();
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
  await expect(entries).toHaveCount(2);
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

test("the bar still fits on one line at the breakpoint the trigger moved", async ({ page }) => {
  await page.setViewportSize({ width: NAV_INLINE_MIN_PX, height: 800 });
  await page.goto(`${project}/tasks`);
  await expect(page.getByRole("navigation", { name: "Primary navigation" })).toBeVisible();

  const measured = await page.evaluate(() => {
    const nav = document.querySelector("nav[aria-label='Primary navigation']")!;
    // The switcher pinned to the 224px a long project name reaches, which is the case
    // NAV_INLINE_MIN_PX is defined against rather than the short one this sandbox has.
    const style = document.createElement("style");
    style.textContent =
      "nav[aria-label='Primary navigation'] > label { flex: 0 0 224px !important; }";
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
});
