import { expect, test, type Page } from "@playwright/test";

/**
 * Pasting a screenshot, through the real stack.
 *
 * The value of running this in a browser is the half no unit test reaches: a real
 * clipboard paste carrying a real image blob, base64 over the wire, a sidecar file
 * written on disk, and the browser fetching that file back and rendering it. Each
 * layer works alone in the suites above; this is the one that proves they agree.
 */

/** A one-pixel PNG, built in the page so the bytes reaching the clipboard are real. */
const PNG_DATA_URL =
  "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==";

/**
 * Paste an image into an element, the way pressing Ctrl+V with a screenshot does.
 *
 * Playwright cannot put an image on the OS clipboard, so the ClipboardEvent is
 * constructed with a real File in its DataTransfer. That is exactly what the browser
 * delivers on a genuine paste, and it is what the component reads.
 */
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

test("pastes a screenshot into a report and renders it back from the stored file", async ({
  page,
  request,
}) => {
  await page.goto("/app/");
  await page.getByRole("button", { name: "Report issue" }).click();
  const dialog = page.getByRole("dialog", { name: "Report an issue" });
  await dialog.getByRole("textbox", { name: /^Title/ }).fill("The status badge is unreadable");
  await dialog.getByRole("textbox", { name: /^What happened/ }).fill("See the attached capture.");

  await pasteImage(page, 'textarea[name="details"]', PNG_DATA_URL);
  await expect(dialog.getByRole("list", { name: "Attached images" })).toBeVisible();

  await dialog.getByRole("button", { name: "File issue" }).click();
  await expect(dialog).toContainText("Filed as");
  const filed = (await dialog.getByRole("status").innerText())
    .replace(/^Filed as\s*/, "")
    .replace(/\.$/, "");

  // The stored record carries metadata only -- the bytes are in a sidecar file.
  const record = await (await request.get(`/api/tasks/${filed}`)).json();
  const attachment = record.log[0].attachments[0];
  expect(attachment.media_type).toBe("image/png");
  expect(attachment.path).toBe(`attachments/${filed}/${attachment.sha256}.png`);
  expect(JSON.stringify(record)).not.toContain("data:image");

  // And the browser renders it where the entry is read.
  await dialog.getByRole("link", { name: "Open the task" }).click();
  const shown = page.getByRole("img", { name: "screenshot.png" });
  await expect(shown).toBeVisible();
  await expect(shown).toHaveAttribute("src", new RegExp(`${attachment.sha256}\\.png$`));
  // Actually decoded by the browser, rather than a broken-image icon.
  expect(await shown.evaluate((image: HTMLImageElement) => image.naturalWidth)).toBeGreaterThan(0);
});

test("an oversized paste is refused and the typed prose survives", async ({ page }) => {
  await page.goto("/app/");
  await page.getByRole("button", { name: "Report issue" }).click();
  const dialog = page.getByRole("dialog", { name: "Report an issue" });
  const prose = "Prose that must still be here after the image is rejected.";
  await dialog.getByRole("textbox", { name: /^Title/ }).fill("Oversized paste");
  await dialog.getByRole("textbox", { name: /^What happened/ }).fill(prose);

  // Six megabytes of PNG-headed data: past the five the server and the widget agree on.
  await page.evaluate(() => {
    const bytes = new Uint8Array(6 * 1024 * 1024);
    bytes.set([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
    const transfer = new DataTransfer();
    transfer.items.add(new File([bytes], "huge.png", { type: "image/png" }));
    const element = document.querySelector('textarea[name="details"]') as HTMLElement;
    element.dispatchEvent(
      new ClipboardEvent("paste", { clipboardData: transfer, bubbles: true, cancelable: true }),
    );
  });

  await expect(dialog.getByRole("alert")).toContainText("over the");
  await expect(dialog.getByRole("textbox", { name: /^What happened/ })).toHaveValue(prose);
  await expect(dialog.getByRole("list", { name: "Attached images" })).toBeHidden();
});
