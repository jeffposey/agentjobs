import { expect, test, type Page } from "@playwright/test";

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
const NAV_INLINE_MIN_PX = 1100;

const SURFACES = [
  ["/app/p/_local", "Dashboard"],
  ["/app/p/_local/tasks", "Tasks"],
  ["/app/p/_local/tasks/new", "Create"],
  ["/app/p/_local/dispatch", "Dispatch"],
  ["/app/p/_local/playbooks", "Playbooks"],
  ["/app/p/_local/runs", "Runs"],
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

test("Create carries no accent of its own, so colour in the bar means one thing", async ({
  page,
}) => {
  // The report's actual cause. On the Dashboard, Create was the only coloured entry
  // in the bar, so it read as the selected tab; it is now painted like every other
  // destination you are not on.
  await page.setViewportSize(DESKTOP);
  await page.goto("/app/p/_local");

  const links = await readLinks(page);
  const create = links.find((link) => link.label === "Create")!;
  const tasks = links.find((link) => link.label === "Tasks")!;
  expect(create.current).toBe(false);
  expect(create.color).toBe(tasks.color);
  expect(create.background).toBe(tasks.background);
});

test("the burger panel marks the current destination too", async ({ page }) => {
  await page.setViewportSize(PHONE);
  expect(PHONE.width).toBeLessThan(NAV_INLINE_MIN_PX);
  await page.goto("/app/p/_local/dispatch");

  await page.getByRole("button", { name: "Navigation" }).click();
  const panel = page.locator("#primary-nav-destinations");
  await expect(panel).toBeVisible();

  const marked = panel.locator('[aria-current="page"]');
  await expect(marked).toHaveCount(1);
  await expect(marked).toHaveText("Dispatch");
  await expect(marked).not.toHaveCSS("background-color", "rgba(0, 0, 0, 0)");
});
