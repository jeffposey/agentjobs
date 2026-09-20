import { expect, test, type Page } from "@playwright/test";

/**
 * A review pass, in a real browser: collect findings, reload, file the lot (task-121).
 *
 * The half no component test reaches is the durability. jsdom has no IndexedDB, so
 * everything about "the list is still there after a reload" is only true here -- real
 * object stores, real screenshot bytes on disk in the browser's profile, and a real
 * navigation between two of the findings so each one's provenance is genuinely its own
 * rather than one the test arranged.
 *
 * The second test is the case retry safety exists for and which nothing else can stage:
 * a create that **succeeded on the server** and whose answer never reached the browser.
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
  await page.getByRole("button", { name: "New task or issue" }).click();
  const dialog = page.getByRole("dialog", { name: "New task" });
  await dialog.getByRole("combobox", { name: "File into project" }).waitFor();
  return dialog;
}

test("collects findings across two pages, survives a reload, and files them in one action", async ({
  page,
  request,
}) => {
  // One finding on the dashboard, with a pasted screenshot.
  await page.goto("/app/p/_local");
  let dialog = await openCapture(page);
  await dialog.getByRole("textbox", { name: /^Title/ }).fill("The dashboard cards overlap");
  await dialog
    .getByRole("textbox", { name: /^What happened/ })
    .fill("At 375px the two cards sit on top of each other.");
  await pasteImage(page, 'textarea[name="details"]', PNG_DATA_URL);
  await expect(dialog.getByRole("list", { name: "Attached images" })).toBeVisible();

  // Ctrl+Enter, which is the gesture the feature is shaped around: the dialog stays
  // open, the form empties, and the caret is back in Title.
  await dialog.getByRole("textbox", { name: /^Title/ }).press("Control+Enter");
  const tray = dialog.getByRole("region", { name: "Collected findings" });
  await expect(tray.getByText("The dashboard cards overlap")).toBeVisible();
  await expect(dialog.getByRole("textbox", { name: /^Title/ })).toHaveValue("");
  await expect(dialog.getByRole("textbox", { name: /^Title/ })).toBeFocused();

  // A second finding, noticed somewhere else entirely. The page is left and returned to,
  // so the provenance of each card is genuinely the page it was collected on.
  await page.keyboard.press("Escape");
  await page.goto("/app/p/_local/tasks");
  dialog = await openCapture(page);
  await dialog.getByRole("textbox", { name: /^Title/ }).fill("The task list wraps the badges");
  await dialog
    .getByRole("textbox", { name: /^What happened/ })
    .fill("Every badge takes a line of its own.");
  await dialog.getByRole("textbox", { name: /^Title/ }).press("Control+Enter");
  await expect(tray.getByText("The task list wraps the badges")).toBeVisible();

  // The accident this is all for. Nothing has been filed yet.
  await page.reload();
  const trigger = page.getByRole("button", { name: "New task or issue (2 collected)" });
  await expect(trigger).toBeVisible();

  dialog = await openCapture(page);
  const restored = dialog.getByRole("region", { name: "Collected findings" });
  await expect(restored.getByText("The dashboard cards overlap")).toBeVisible();
  await expect(restored.getByText("The task list wraps the badges")).toBeVisible();
  // The screenshot came back out of the store, and the browser decoded it -- so the
  // bytes survived the reload rather than only the prose.
  const thumbnail = restored.getByRole("img", { name: "screenshot.png" });
  await expect(thumbnail).toBeVisible();
  await expect
    .poll(async () => thumbnail.evaluate((image: HTMLImageElement) => image.naturalWidth))
    .toBeGreaterThan(0);

  // One action.
  await dialog.getByRole("button", { name: "Create 2 tasks" }).click();
  const filed = dialog.getByRole("region", { name: "Tasks created" });
  await expect(filed).toBeVisible();
  await expect(filed.getByRole("listitem")).toHaveCount(2);
  // The tray is empty, and stays empty across a reload: a confirmed success is removed
  // from the device as well as from the screen.
  await expect(dialog.getByRole("region", { name: "Collected findings" })).toBeHidden();
  await page.reload();
  await expect(page.getByRole("button", { name: "New task or issue" })).toHaveAccessibleName(
    "New task or issue",
  );

  // The stored records, read from the server rather than from the page that made them.
  const tasks = (await (await request.get("/api/projects/_local/tasks")).json()) as Array<{
    id: string;
    title: string;
  }>;
  const byTitle = (title: string) => tasks.find((task) => task.title === title);
  const overlap = byTitle("The dashboard cards overlap");
  const wrapped = byTitle("The task list wraps the badges");
  expect(overlap).toBeDefined();
  expect(wrapped).toBeDefined();

  const overlapRecord = await (await request.get(`/api/tasks/${overlap!.id}`)).json();
  const wrappedRecord = await (await request.get(`/api/tasks/${wrapped!.id}`)).json();

  // Each task carries the page *its own* finding was noticed on, not the page the batch
  // was sent from -- which was the task list for both of them.
  expect(overlapRecord.spec.description).toContain("/p/_local");
  expect(overlapRecord.spec.description).not.toContain("/p/_local/tasks");
  expect(wrappedRecord.spec.description).toContain("/p/_local/tasks");

  // And they are ordinary reported issues: same tag, same attribution, same lifecycle a
  // single capture produces.
  for (const record of [overlapRecord, wrappedRecord]) {
    expect(record.tags).toEqual(["reported-issue"]);
    expect(record.log[0].actor).toBe("E2E Human");
    expect(record.lifecycle).toBe("draft");
  }

  // The screenshot reached a sidecar file, exactly as a single report's does.
  const attachment = overlapRecord.log[0].attachments[0];
  expect(attachment.media_type).toBe("image/png");
  expect(attachment.path).toBe(`attachments/${overlap!.id}/${attachment.sha256}.png`);
  expect(JSON.stringify(overlapRecord)).not.toContain("data:image");
  // And the finding that had no screenshot did not inherit the other one's. A log entry
  // with nothing attached carries `null` rather than an empty list.
  expect(wrappedRecord.log[0].attachments).toBeNull();
});

test("a create whose answer was lost is retried without making a second task", async ({
  page,
  request,
}) => {
  // The case removal alone cannot cover, and the reason each item carries the
  // `operation_id` it was collected with: the request reached the server and made a
  // task, and the response never came back. The browser can only conclude it failed.
  await page.goto("/app/p/_local");
  let dialog = await openCapture(page);
  await dialog.getByRole("textbox", { name: /^Title/ }).fill("Answer lost on the way back");
  await dialog
    .getByRole("textbox", { name: /^What happened/ })
    .fill("The create landed; the response did not.");
  await dialog.getByRole("textbox", { name: /^Title/ }).press("Control+Enter");
  await expect(
    dialog.getByRole("region", { name: "Collected findings" }).getByText("Answer lost on the way back"),
  ).toBeVisible();

  // Let the first create through to the server, then take its answer away.
  let swallow = true;
  await page.route("**/api/projects/_local/tasks", async (route) => {
    if (route.request().method() !== "POST" || !swallow) {
      await route.continue();
      return;
    }
    swallow = false;
    await route.fetch();
    await route.abort("connectionreset");
  });

  await dialog.getByRole("button", { name: "Create 1 task" }).click();
  await expect(dialog.getByText(/Nothing was created/)).toBeVisible();
  // The card is still here with its prose, which is what the person needs to retry.
  await expect(
    dialog.getByRole("region", { name: "Collected findings" }).getByRole("alert"),
  ).toBeVisible();

  // The server, meanwhile, has one task.
  const countMatching = async () => {
    const tasks = (await (await request.get("/api/projects/_local/tasks")).json()) as Array<{
      title: string;
    }>;
    return tasks.filter((task) => task.title === "Answer lost on the way back").length;
  };
  expect(await countMatching()).toBe(1);

  // Press the button again. The same operation_id goes back, so the server resolves it
  // to the task the first attempt made rather than creating a second.
  await dialog.getByRole("button", { name: "Create 1 task" }).click();
  const filed = dialog.getByRole("region", { name: "Tasks created" });
  await expect(filed).toBeVisible();
  await expect(dialog.getByRole("region", { name: "Collected findings" })).toBeHidden();
  expect(await countMatching()).toBe(1);

  // And the id the page now links is that one task, so the person is sent to the record
  // that exists rather than to a duplicate.
  const linked = await filed.getByRole("listitem").first().getAttribute("data-task-id");
  const record = await (await request.get(`/api/tasks/${linked}`)).json();
  expect(record.title).toBe("Answer lost on the way back");

  // Nothing is left on the device, so the next page load does not offer to file it again.
  await page.reload();
  await expect(page.getByRole("button", { name: "New task or issue" })).toHaveAccessibleName(
    "New task or issue",
  );
  void dialog;
});
