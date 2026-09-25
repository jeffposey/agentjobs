import { readFile, writeFile } from "node:fs/promises";
import { resolve } from "node:path";

import { expect, test, type Page, type Request } from "./fixtures";

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
async function rebuildFrontend(page: Page, revision: string, dist: string) {
  const swPath = resolve(dist, "sw.js");
  const infoPath = resolve(dist, "build-info.json");
  const sw = await readFile(swPath, "utf8");
  const info = await readFile(infoPath, "utf8");
  await writeFile(swPath, `${sw}\n// rebuilt for ${revision}\n`, "utf8");
  await writeFile(infoPath, `${JSON.stringify({ bundle_id: revision }, null, 2)}\n`, "utf8");
  await requestUpdate(page);
  return async () => {
    await writeFile(swPath, sw, "utf8");
    await writeFile(infoPath, info, "utf8");
  };
}

/**
 * Ask the browser to check for a new `sw.js`, without waiting for the answer.
 *
 * Fire and forget: the reload this may provoke destroys the page's execution context,
 * so awaiting the update inside the page would fail for the wrong reason -- and so would
 * this evaluate, if that reload is already under way when it arrives.
 */
async function requestUpdate(page: Page) {
  try {
    await page.evaluate(() => {
      void navigator.serviceWorker
        .getRegistration("/app/")
        .then((registration) => registration?.update())
        .catch(() => undefined);
    });
  } catch {
    // The page is navigating, which is the outcome being asked for.
  }
}

/**
 * Keep asking for the update until `done` says the new worker has taken the tab.
 *
 * **One request is not enough, and was the flake** (task-515). An update check the
 * browser already has in flight absorbs a second one, and if that check fetched `sw.js`
 * before the rebuild wrote it, it finds nothing new and the tab is never taken over.
 * The test then waited out its whole timeout for a reload nobody was going to cause:
 * red in seven finish gates in four days, each at exactly 20 seconds, when the reload
 * takes about 1.3 seconds whenever it happens at all. Reproduced by starting a check
 * just before the write; measured 2026-09-24.
 *
 * So each poll asks again. The timeout is still a clock, but it now bounds a process
 * that is retried rather than one event that can be lost, and a red here means the
 * takeover failed on every one of those checks -- which is a real finding.
 */
async function untilTakenOver(page: Page, done: () => Promise<boolean>) {
  try {
    await expect
      .poll(
        async () => {
          if (await done()) return true;
          await requestUpdate(page);
          return false;
        },
        { timeout: 20_000, intervals: [1_000] },
      )
      .toBe(true);
  } catch (error) {
    throw new Error(
      `${String(error)}\n\nWhat the page could see: ${await workerState(page)}` +
        `\nRequests still in flight: ${pendingRequests(page)}` +
        `\nWhat the browser says of each worker: ${workerVersions(page)}`,
    );
  }
}

const inFlight = new WeakMap<Page, Map<Request, number>>();

/**
 * Record the page's requests from now on, so a red can name the ones still unfinished.
 *
 * A request the old worker is answering is an event it has not finished, and the
 * browser does not activate a waiting worker until the active one has none -- even
 * after `skipWaiting()` (task-582). So when the new worker sits in `waiting`, what is
 * still in flight is the first thing to read.
 */
function trackRequests(page: Page) {
  const pending = new Map<Request, number>();
  inFlight.set(page, pending);
  // The context, not the page: a request a worker answers is not a page event at all,
  // and the worker's own `fetch` is reported only here. Tracking the page missed
  // exactly the requests this exists to find.
  const context = page.context();
  context.on("request", (request) => pending.set(request, Date.now()));
  context.on("requestfinished", (request) => pending.delete(request));
  context.on("requestfailed", (request) => pending.delete(request));
}

const versions = new WeakMap<Page, Map<string, string>>();

/**
 * Watch every worker version through the DevTools protocol, which sees what the page
 * cannot: whether the old worker is running, starting or stopping when the new one is
 * held. A red with nothing in flight (task-582, after the `/api/` fix) had only the
 * page's view, and that could not say what the active worker was busy with.
 */
async function trackWorkerVersions(page: Page) {
  const seen = new Map<string, string>();
  versions.set(page, seen);
  const session = await page.context().newCDPSession(page);
  session.on("ServiceWorker.workerVersionUpdated", ({ versions: updated }) => {
    for (const version of updated) {
      const script = version.scriptURL.split("/").pop();
      seen.set(
        version.versionId,
        `${script} ${version.status} ${version.runningStatus} clients=${
          version.controlledClients?.length ?? 0
        }`,
      );
    }
  });
  await session.send("ServiceWorker.enable");
}

function workerVersions(page: Page): string {
  const seen = versions.get(page);
  return seen ? JSON.stringify(Object.fromEntries(seen)) : "not tracked";
}

function pendingRequests(page: Page): string {
  const pending = inFlight.get(page);
  if (!pending) return "not tracked";
  const now = Date.now();
  return JSON.stringify(
    [...pending].map(([request, started]) => ({
      url: request.url(),
      ageMs: now - started,
    })),
  );
}

/**
 * Everything a red `untilTakenOver` needs in order to say which half failed (task-571).
 *
 * "The tab was never reloaded" has at least three causes that look identical from the
 * assertion: the browser never installed the new worker, installed it and did not hand
 * it the tab, or handed it the tab and the page declined to reload. The registration's
 * three slots and the takeover count tell them apart.
 */
async function workerState(page: Page): Promise<string> {
  try {
    return await page.evaluate(async () => {
      const slot = (worker: ServiceWorker | null | undefined) =>
        worker ? `${worker.state} ${worker.scriptURL}` : "none";
      const registration = await navigator.serviceWorker.getRegistration("/app/");
      const probe = window as unknown as { __takeovers?: number; __survived?: string };
      const served = await fetch("/app/sw.js", { cache: "no-store" })
        .then((response) => response.text())
        .then((text) => text.trim().split("\n").pop())
        .catch((error: unknown) => `fetch failed: ${String(error)}`);
      return JSON.stringify({
        controller: slot(navigator.serviceWorker.controller),
        installing: slot(registration?.installing),
        waiting: slot(registration?.waiting),
        active: slot(registration?.active),
        takeovers: probe.__takeovers ?? "not counted",
        survived: probe.__survived ?? "fresh document",
        servedSwLastLine: served,
      });
    });
  } catch (error) {
    return `unreadable (${String(error)})`;
  }
}

/** Whether the tab is a fresh document; `false` while it is between the two. */
async function reloaded(page: Page) {
  try {
    return await page.evaluate(
      () => (window as unknown as { __survived?: string }).__survived === undefined,
    );
  } catch {
    return false;
  }
}

/** Count the times a worker takes this tab over, from now on. */
async function countTakeovers(page: Page) {
  await page.evaluate(() => {
    const counter = window as unknown as { __takeovers?: number };
    counter.__takeovers = 0;
    navigator.serviceWorker.addEventListener("controllerchange", () => {
      counter.__takeovers = (counter.__takeovers ?? 0) + 1;
    });
  });
}

function takeovers(page: Page) {
  return page.evaluate(() => (window as unknown as { __takeovers?: number }).__takeovers ?? 0);
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

test("a rebuild still reloads a tab where nobody is typing", async ({ page, bundleDir }) => {
  // The unchanged half of the behaviour, and the reason the guard is a guard rather
  // than a switch: a stale bundle talking to a new server is a real problem, and an
  // idle tab is exactly where reloading it costs nothing.
  trackRequests(page);
  await trackWorkerVersions(page);
  await page.goto("/app/p/_local");
  await waitForControl(page);
  await markPage(page);
  await countTakeovers(page);

  // Let the page settle before the rebuild (task-582). Rebuilding straight after
  // `waitForControl` put the new worker's install ~150 ms after the first worker's,
  // inside the page's startup burst of API calls, and 3 of 20 runs then held the new
  // worker in `waiting` for the whole 20 seconds, with nothing in flight and both
  // workers running. Why the browser never activated it is not known. Waiting for
  // `networkidle` first: 0 of 40, and a fixed 3 s wait, 0 of 20. Waiting
  // for the first worker to reach `activated` did not help (3 of 20). A real rebuild
  // never lands in a tab's first second, so this removes a state the test made rather
  // than one a person meets.
  await page.waitForLoadState("networkidle");
  const restore = await rebuildFrontend(page, "idletab00000", bundleDir);
  try {
    await untilTakenOver(page, () => reloaded(page));
    expect(await marked(page)).toBe(false);
    await expect(page.getByRole("navigation", { name: "Primary navigation" })).toBeVisible();
  } finally {
    await restore();
  }
});

test("a rebuild reloads an idle tab even while one of its API calls is hanging", async ({
  page,
  bundleDir,
}) => {
  // Register row 16 (task-582). Chromium does not activate a waiting worker while the
  // active one still has an event in flight, `skipWaiting()` or not. The worker used to
  // answer every `/api/` GET with `respondWith(fetch(request))`, so one slow API call
  // held the rebuilt worker in `waiting` for as long as it took -- the exact state a
  // finish gate caught. The call is held open here on purpose, from the page's own
  // `fetch`, and routed so it never reaches the server and never completes.
  await page.context().route("**/api/task-582-hang", () => undefined);
  trackRequests(page);
  await trackWorkerVersions(page);
  await page.goto("/app/p/_local");
  await waitForControl(page);
  await page.evaluate(() => {
    void fetch("/api/task-582-hang").catch(() => undefined);
  });
  await expect.poll(() => pendingRequests(page)).toContain("task-582-hang");
  await markPage(page);
  await countTakeovers(page);

  const restore = await rebuildFrontend(page, "hungcall0000", bundleDir);
  try {
    await untilTakenOver(page, () => reloaded(page));
    expect(await marked(page)).toBe(false);
  } finally {
    await restore();
  }
});

test("a rebuild does not take a tab that is holding unsent text; the banner offers it", async ({
  page,
  bundleDir,
}) => {
  await page.goto("/app/p/_local");
  await waitForControl(page);

  const dialog = await openCapture(page);
  await dialog.getByRole("textbox", { name: /^Title/ }).fill("Mid-sentence");
  await dialog
    .getByRole("textbox", { name: /^What happened/ })
    .fill("Half a thought, and nobody has pressed anything yet.");
  await markPage(page);
  await countTakeovers(page);

  const restore = await rebuildFrontend(page, "typingtab000", bundleDir);
  try {
    // The new worker genuinely takes control -- this is not a test of a rebuild that
    // did not happen -- and the page stays exactly where it was. Counted rather than
    // read off `controller`, which was already set before the rebuild: until task-515
    // this waited on that, so a rebuild the browser never noticed passed here.
    await untilTakenOver(page, async () => (await takeovers(page)) > 0);
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
