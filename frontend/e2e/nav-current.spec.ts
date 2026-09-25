import { expect, test, type Page } from "./fixtures";

/**
 * task-336: the primary nav says which destination you are looking at.
 *
 * The reported symptom was that nothing in the bar marked the current page -- and
 * worse, that the one entry which *was* coloured (Create, `text-blue-300`) read as
 * the selected tab from the Dashboard. So the interesting assertions are about what
 * the browser actually paints, which is why they are here rather than only in
 * `PrimaryNav.test.tsx`: jsdom loads no stylesheet, so every link there reports the
 * same computed colour whatever its class list says, and a class-name assertion would
 * pass just as happily against a class Tailwind never emitted.
 *
 * Which entry is current for a given URL is a pure function and is exhaustively
 * covered in the unit test. This file asks the narrower question a browser can answer:
 * is exactly one entry marked, and does it look different from the rest.
 */

const DESKTOP = { width: 1280, height: 800 };
/** iPhone 14/15 CSS pixels, where this app is read over Tailscale. */
const PHONE = { width: 390, height: 844 };

/** Mirrors `NAV_INLINE_MIN_PX`; below it the destinations are behind the burger. */
const NAV_INLINE_MIN_PX = 770;

const SURFACES = [
  ["/app/p/_local", "Dashboard"],
  ["/app/p/_local/tasks", "Tasks"],
] as const;

/**
 * Routes the bar no longer has an entry for (task-345).
 *
 * All three still work and all three are reached from the actions menu; what changed
 * is the bar's answer to "where am I" on them, which is now *nothing*. See ac-4.
 */
const UNOWNED_SURFACES = [
  "/app/p/_local/dispatch",
  "/app/p/_local/playbooks",
  "/app/p/_local/analytics",
] as const;

/** The nav, addressed by its label rather than by a tag: pages carry headers too. */
function nav(page: Page) {
  return page.getByRole("navigation", { name: "Primary navigation" });
}

/** Every nav link's label, colours and current-ness, read from the browser itself. */
async function readLinks(page: Page) {
  return page.evaluate(() => {
    const bar = document.querySelector('nav[aria-label="Primary navigation"]');
    const links = [...(bar?.querySelectorAll("a") ?? [])];
    return links.map((link) => {
      const style = getComputedStyle(link);
      return {
        label: (link.textContent ?? "").trim(),
        current: link.getAttribute("aria-current") === "page",
        color: style.color,
        background: style.backgroundColor,
        // `inset-ring-1` paints through box-shadow, so an empty value here is the
        // ring having failed to compile -- which a class-name check cannot see.
        shadow: style.boxShadow,
      };
    });
  });
}

test("every surface marks exactly one destination, and it is the one you are on", async ({
  page,
}) => {
  await page.setViewportSize(DESKTOP);

  for (const [surface, label] of SURFACES) {
    await page.goto(surface);
    await expect(nav(page)).toBeVisible();

    const marked = nav(page).locator('[aria-current="page"]');
    // One, and only one: two marked entries answer "which tab is selected" no better
    // than none did.
    await expect(marked, `${surface} marks one destination`).toHaveCount(1);
    await expect(marked).toHaveText(new RegExp(`^${label}`));
  }
});

test("the marked destination is painted differently from the rest", async ({ page }) => {
  await page.setViewportSize(DESKTOP);
  await page.goto("/app/p/_local");

  const links = await readLinks(page);
  const current = links.filter((link) => link.current);
  const rest = links.filter((link) => !link.current);
  expect(current).toHaveLength(1);
  expect(rest.length).toBeGreaterThan(0);

  const marked = current[0]!;
  expect(marked.label).toBe("Dashboard");
  // A transparent background would mean the tint never compiled; equally, matching a
  // sibling on every channel would mean it is not distinguished at all.
  expect(marked.background).not.toBe("rgba(0, 0, 0, 0)");
  expect(marked.shadow === "none", "the current entry carries its inset ring").toBe(false);
  for (const other of rest) {
    expect(other.background, `${other.label} is not tinted like the current entry`).not.toBe(
      marked.background,
    );
    expect(other.color, `${other.label} is dimmer than the current entry`).not.toBe(marked.color);
  }
});

test("the paint assertion has teeth: rendering every entry alike makes it fail", async ({
  page,
}) => {
  // A negative control in the test rather than in a reviewer's head, the same shape
  // pinned-header.spec.ts uses for the pin. It reproduces the reported bug exactly --
  // every destination painted identically -- by giving the current entry a sibling's
  // class list, and shows that the check above then has nothing to find.
  //
  // Copying a sibling's classes rather than deleting the marking ones: deleting them
  // leaves the link with no colour class at all, so it inherits the page's brighter
  // text and stays distinguishable from its muted neighbours by accident. That would
  // be a control that fails for a reason unrelated to what it is controlling for.
  await page.setViewportSize(DESKTOP);
  await page.goto("/app/p/_local");

  await page.evaluate(() => {
    const bar = document.querySelector('nav[aria-label="Primary navigation"]');
    const marked = bar?.querySelector('[aria-current="page"]');
    const other = bar?.querySelector("a:not([aria-current])");
    if (marked && other) marked.className = other.className;
  });

  const links = await readLinks(page);
  const marked = links.find((link) => link.current)!;
  const other = links.find((link) => !link.current)!;
  expect(marked.background).toBe(other.background);
  expect(marked.color).toBe(other.color);
});

test("no action in the bar is painted like a place, so colour still means one thing", async ({
  page,
}) => {
  // The report's actual cause. On the Dashboard, Create was the only coloured entry in
  // the bar, so it read as the selected tab. task-346 removed that link and put the act
  // behind a button, which must not reintroduce the accent by another route -- so the
  // capture trigger is painted like the actions kebab beside it, not like the entry you
  // are on.
  await page.setViewportSize(DESKTOP);
  await page.goto("/app/p/_local");

  expect((await readLinks(page)).map((link) => link.label)).not.toContain("Create");

  const painted = await page.evaluate(() => {
    const read = (label: string) => {
      const element = document.querySelector<HTMLElement>(`button[aria-label="${label}"]`)!;
      return getComputedStyle(element).color;
    };
    return { capture: read("New task or issue"), actions: read("Actions") };
  });
  expect(painted.capture).toBe(painted.actions);

  // And the entry you are on is still the brightest thing in the row.
  const links = await readLinks(page);
  const current = links.find((link) => link.current)!;
  expect(current.label).toBe("Dashboard");
  expect(current.color).not.toBe(painted.capture);
});

test("the burger panel marks the current destination too", async ({ page }) => {
  await page.setViewportSize(PHONE);
  expect(PHONE.width).toBeLessThan(NAV_INLINE_MIN_PX);
  await page.goto("/app/p/_local/tasks");

  await page.getByRole("button", { name: "Navigation" }).click();
  const panel = page.locator("#primary-nav-destinations");
  await expect(panel).toBeVisible();

  const marked = panel.locator('[aria-current="page"]');
  await expect(marked).toHaveCount(1);
  await expect(marked).toHaveText("Tasks");
  await expect(marked).not.toHaveCSS("background-color", "rgba(0, 0, 0, 0)");
});

test("a route with no entry marks nothing, rather than lighting up the Dashboard", async ({
  page,
}) => {
  // ac-4, measured. Until task-345, `currentDestinationPath` fell back to Dashboard's
  // `""` for anything under the project no other entry claimed -- harmless while every
  // route had an entry, and a confident lie the moment Dispatch settings and Playbooks
  // moved into the actions menu. Marking nothing is recoverable; marking the wrong
  // thing is task-336's original defect again.
  for (const viewport of [DESKTOP, PHONE]) {
    await page.setViewportSize(viewport);
    for (const surface of UNOWNED_SURFACES) {
      await page.goto(surface);
      await expect(nav(page)).toBeVisible();
      await expect(
        nav(page).locator('[aria-current="page"]'),
        `${surface} at ${viewport.width}px marks no entry in the bar`,
      ).toHaveCount(0);

      if (viewport.width >= NAV_INLINE_MIN_PX) continue;
      // The panel is the phone's whole bar, so an unmarked row there is the half that
      // actually matters on the surface this app is read on.
      await page.getByRole("button", { name: "Navigation" }).click();
      const panel = page.locator("#primary-nav-destinations");
      await expect(panel).toBeVisible();
      await expect(panel.locator('[aria-current="page"]')).toHaveCount(0);
    }
  }
});
