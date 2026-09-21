import { readFile, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { expect, test, type Page } from "@playwright/test";

/**
 * The capture form across a page that goes away (task-512).
 *
 * The findings typed here carry titles nothing else files. One temporary project serves
 * the whole Playwright run, so two specs that collect the same sentence file two tasks
 * the other one can find -- which is how `capture-tray` came to read this file's
 * provenance and fail on it.
 *
 * Everything here needs a real browser and nothing else will do: a real service worker
 * taking control of a real tab, and a real IndexedDB holding what was typed. jsdom has
 * neither, so a unit test of either half would be a test of a shim.
 *
 * The reload these tests are about was **observed here before it was changed**. An
 * earlier revision of this file typed half a finding, rebuilt the worker, and watched
 * the tab reload with no prompt and the finding gone -- which is what "the page goes
 * away for reasons that are not yours" meant in practice. What survives that
 * demonstration is the pair below: an idle tab still reloads, a tab holding unsent text
 * does not.
 */

/** A one-pixel PNG, built in the page so the bytes reaching the clipboard are real. */
const PNG_DATA_URL =
  "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==";

/** Paste an image into an element, the way pressing Ctrl+V with a screenshot does. */
async function pasteImage(page: Page, selector: string, dataUrl: string) {
  await page.evaluate(
    async ([target, url]) => {
      const blob = await (await fetch(url)).blob();
      const transfer = new DataTransfer();
      transfer.items.add(new File([blob], "screenshot.png", { type: "image/png" }));
      const element = document.querySelector(target) as HTMLElement;
      element.focus();
      element.dispatchEvent(
        new ClipboardEvent("paste", { clipboardData: transfer, bubbles: true, cancelable: true }),
      );
    },
    [selector, dataUrl] as const,
  );
}

/** Open the capture dialog and wait for the destination to resolve. */
async function openCapture(page: Page) {
  await page.getByRole("button", { name: /^New task or issue/ }).click();
  const dialog = page.getByRole("dialog", { name: "New task" });
  await dialog.getByRole("combobox", { name: "File into project" }).waitFor();
  return dialog;
}

/**
 * The stored draft, read out of IndexedDB by the test rather than through the app.
 *
 * Waiting for a page to render what it just typed proves nothing about durability --
 * the value was already in React state. Reading the database is what says the reload
 * about to happen has something to come back to, which is also why these tests never
 * reload on a timer.
 */
async function draftOnDisk(page: Page): Promise<string | null> {
  return page.evaluate(
    () =>
      new Promise<string | null>((resolve) => {
        const request = indexedDB.open("agentjobs-capture", 2);
        request.onerror = () => resolve(null);
        request.onsuccess = () => {
          try {
            const read = request.result
              .transaction("draft", "readonly")
              .objectStore("draft")
              .get("capture");
            read.onsuccess = () => resolve(read.result ? JSON.stringify(read.result) : null);
            read.onerror = () => resolve(null);
          } catch {
            resolve(null);
          }
        };
      }),
  );
}

/** The packaged build this checkout's server is serving. */
const DIST = resolve(
  dirname(fileURLToPath(import.meta.url)),
  "..",
  "..",
  "src",
  "agentjobs",
  "frontend_dist",
);

/** Wait until a service worker is actually driving this tab. */
async function waitForControl(page: Page) {
  await page.waitForFunction(() => Boolean(navigator.serviceWorker.controller), null, {
    timeout: 20_000,
  });
}

/**
 * Rebuild the frontend, as far as an open tab can tell.
 *
 * `npm run build` writes two files an open tab can notice: a new `sw.js` at the same
 * URL, and a new `bundle_id` in `build-info.json` which `/api/version` reads from disk
 * on every call. This changes both, because both halves of the behaviour under test
 * depend on them -- the worker takes the tab over, and the skew banner is what should
 * then offer the reload.
 *
 * The appended line is a comment, so the worker that takes over behaves identically to
 * the one it replaced. Registering a second script URL would have been easier and would
 * have tested a near-neighbour of the mechanism rather than the mechanism.
 *
 * Returns the undo. `frontend_dist/` is a gitignored build artefact either way, but a
 * test that leaves the tree different from how it found it is a test that fails
 * somewhere else.
 */
async function rebuildFrontend(page: Page, revision: string) {
  const swPath = resolve(DIST, "sw.js");
  const infoPath = resolve(DIST, "build-info.json");
  const sw = await readFile(swPath, "utf8");
  const info = await readFile(infoPath, "utf8");
  await writeFile(swPath, `${sw}\n// rebuilt for ${revision}\n`, "utf8");
  await writeFile(infoPath, `${JSON.stringify({ bundle_id: revision }, null, 2)}\n`, "utf8");
  // Fire and forget: the reload this may provoke destroys the page's execution context,
  // so awaiting the update inside the page would fail for the wrong reason.
  await page.evaluate(() => {
    void navigator.serviceWorker.getRegistration("/app/").then((registration) => {
      void registration?.update();
    });
  });
  return async () => {
    await writeFile(swPath, sw, "utf8");
    await writeFile(infoPath, info, "utf8");
  };
}

/** A value on `window` that only a fresh document is without. */
async function markPage(page: Page) {
  await page.evaluate(() => {
    (window as unknown as { __survived?: string }).__survived = "yes";
  });
}

function marked(page: Page) {
  return page.evaluate(() => (window as unknown as { __survived?: string }).__survived === "yes");
}

test("a finding half typed survives a reload, screenshot and specification included", async ({
  page,
}) => {
  await page.goto("/app/p/_local");
  let dialog = await openCapture(page);

  await dialog.getByRole("textbox", { name: /^Title/ }).fill("The draft outlived the page");
  await dialog
    .getByRole("textbox", { name: /^What happened/ })
    .fill("Half typed, and then the page went away.");
  await pasteImage(page, 'textarea[name="details"]', PNG_DATA_URL);
  await expect(dialog.getByRole("list", { name: "Attached images" })).toBeVisible();

  // The half held in the DOM rather than in React state, and the half a reader of the
  // restored form has to be able to see: the section comes back open.
  await dialog.getByRole("button", { name: "Add the full specification" }).click();
  await dialog
    .getByRole("textbox", { name: /^Summary/ })
    .fill("A finding that was being written when the tab reloaded.");

  await expect.poll(() => draftOnDisk(page)).toContain("The draft outlived the page");

  // The accident. Nothing has been collected and nothing has been filed.
  await page.reload();

  dialog = await openCapture(page);
  await expect(dialog.getByRole("textbox", { name: /^Title/ })).toHaveValue(
    "The draft outlived the page",
  );
  await expect(dialog.getByRole("textbox", { name: /^What happened/ })).toHaveValue(
    "Half typed, and then the page went away.",
  );
  await expect(dialog.getByRole("textbox", { name: /^Summary/ })).toHaveValue(
    "A finding that was being written when the tab reloaded.",
  );

  // The screenshot came back out of the store and the browser decoded it, so the bytes
  // survived rather than only the prose.
  const thumbnail = dialog.getByRole("img", { name: "screenshot.png" });
  await expect(thumbnail).toBeVisible();
  await expect
    .poll(async () => thumbnail.evaluate((image: HTMLImageElement) => image.naturalWidth))
    .toBeGreaterThan(0);

  // And it is still one finding: filing it now files what was typed, once.
  await dialog.getByRole("button", { name: "File it" }).click();
  await expect(dialog.getByText(/Filed as/)).toBeVisible();
});

test("collecting a finding clears its draft, so a reload shows the tray and an empty form", async ({
  page,
  request,
}) => {
  await page.goto("/app/p/_local");
  let dialog = await openCapture(page);
  await dialog.getByRole("textbox", { name: /^Title/ }).fill("Collected before the reload");
  await dialog
    .getByRole("textbox", { name: /^What happened/ })
    .fill("This one reached the list; its draft must not.");
  await expect.poll(() => draftOnDisk(page)).toContain("Collected before the reload");

  await dialog.getByRole("textbox", { name: /^Title/ }).press("Control+Enter");
  const tray = dialog.getByRole("region", { name: "Collected findings" });
  await expect(tray.getByText("Collected before the reload")).toBeVisible();
  // Gone from the device the moment it became a tray item, not 400ms later: that gap is
  // the only window in which a reload could offer a finding already on the list.
  await expect.poll(() => draftOnDisk(page)).toBeNull();

  await page.reload();
  dialog = await openCapture(page);
  const restored = dialog.getByRole("region", { name: "Collected findings" });
  await expect(restored.getByText("Collected before the reload")).toHaveCount(1);
  await expect(dialog.getByRole("textbox", { name: /^Title/ })).toHaveValue("");

  // One finding in, one task out.
  await dialog.getByRole("button", { name: "Create 1 task" }).click();
  await expect(dialog.getByRole("region", { name: "Tasks created" })).toBeVisible();
  const tasks = (await (await request.get("/api/projects/_local/tasks")).json()) as Array<{
    title: string;
  }>;
  expect(tasks.filter((task) => task.title === "Collected before the reload")).toHaveLength(1);
});

test("a rebuild still reloads a tab where nobody is typing", async ({ page }) => {
  // The unchanged half of the behaviour, and the reason the guard is a guard rather
  // than a switch: a stale bundle talking to a new server is a real problem, and an
  // idle tab is exactly where reloading it costs nothing.
  await page.goto("/app/p/_local");
  await waitForControl(page);
  await markPage(page);

  const restore = await rebuildFrontend(page, "idletab00000");
  try {
    await page.waitForFunction(
      () => (window as unknown as { __survived?: string }).__survived === undefined,
      null,
      { timeout: 20_000 },
    );
    expect(await marked(page)).toBe(false);
    await expect(page.getByRole("navigation", { name: "Primary navigation" })).toBeVisible();
  } finally {
    await restore();
  }
});

test("a rebuild does not take a tab that is holding unsent text; the banner offers it", async ({
  page,
}) => {
  await page.goto("/app/p/_local");
  await waitForControl(page);

  const dialog = await openCapture(page);
  await dialog.getByRole("textbox", { name: /^Title/ }).fill("Mid-sentence");
  await dialog
    .getByRole("textbox", { name: /^What happened/ })
    .fill("Half a thought, and nobody has pressed anything yet.");
  await markPage(page);

  const restore = await rebuildFrontend(page, "typingtab000");
  try {
    // The new worker genuinely takes control -- this is not a test of a rebuild that
    // did not happen -- and the page stays exactly where it was.
    await page.waitForFunction(
      () =>
        navigator.serviceWorker.controller?.scriptURL !== undefined &&
        (window as unknown as { __survived?: string }).__survived === "yes",
      null,
      { timeout: 10_000 },
    );
    await page.waitForTimeout(2_000);
    expect(await marked(page)).toBe(true);
    await expect(page.getByRole("dialog", { name: "New task" })).toBeVisible();
    await expect(dialog.getByRole("textbox", { name: /^Title/ })).toHaveValue("Mid-sentence");

    // What does the asking instead. `/api/version` reads the bundle id from disk on
    // every call, so the rebuild above is visible to the next poll; the poll is a
    // minute apart by design, so the refetch is provoked rather than waited out.
    // React Query listens for `visibilitychange` on the window, not on the document.
    await page.evaluate(() => window.dispatchEvent(new Event("visibilitychange")));
    const banner = page.getByText("A newer build of AgentJobs");
    await expect(banner).toBeVisible({ timeout: 15_000 });
    await expect(page.getByRole("button", { name: "Reload" })).toBeVisible();
    // Still nothing taken away: the offer is a button, not an event.
    expect(await marked(page)).toBe(true);
  } finally {
    await restore();
  }
});

test("files from a browser that refuses IndexedDB, losing only durability", async ({
  page,
  request,
}) => {
  // ac-4, in the browser rather than in jsdom: a private window, blocked site data, or
  // a profile that has run out of quota. The capture is what must not be lost.
  await page.addInitScript(() => {
    Object.defineProperty(window, "indexedDB", { get: () => undefined, configurable: true });
  });

  await page.goto("/app/p/_local");
  const dialog = await openCapture(page);
  await dialog.getByRole("textbox", { name: /^Title/ }).fill("Filed without a store");
  await dialog
    .getByRole("textbox", { name: /^What happened/ })
    .fill("No IndexedDB here, and the form still files.");
  await dialog.getByRole("button", { name: "File it" }).click();
  await expect(dialog.getByText(/Filed as/)).toBeVisible();

  const tasks = (await (await request.get("/api/projects/_local/tasks")).json()) as Array<{
    title: string;
  }>;
  expect(tasks.some((task) => task.title === "Filed without a store")).toBe(true);
});
