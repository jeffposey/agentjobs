import { expect, test } from "@playwright/test";

/**
 * The microphone, in the real application rather than in jsdom.
 *
 * Nothing here speaks: there is no microphone on the machines this runs on, and an
 * automated gesture would not be a person's gesture anyway. What it holds is the two
 * things a refactor of the capture form is most likely to lose without anyone noticing,
 * and the one thing that would be a defect if it ever started happening.
 *
 * **Why a refactor is the risk worth spending a test on.** task-172's wiring was
 * written against `IssueReporter` and `TaskCreate`, and task-346 replaced both with
 * `CaptureForm` while the branch was open. The control survived that because it takes a
 * resolver rather than a ref -- but the *wiring* had to be re-applied by hand, and the
 * failure mode of forgetting is a form that looks completely normal.
 *
 * **Note the suite runs with Chromium's on-device speech component switched off**, so
 * `SpeechRecognition.available` is absent here and the control takes the same path
 * Safari and every pre-139 Chrome take. See `playwright.config.ts` for why: in a
 * headless Chromium that component's availability query crashes the renderer.
 */

/** Every field the capture form gives a microphone, by the button's accessible name. */
const DICTATED = [
  "Dictate into Title",
  "Dictate into What happened",
  "Dictate into Summary",
  "Dictate into Intent",
  "Dictate into Constraints",
  "Dictate into Out of scope",
  "Dictate into Acceptance criteria",
];

test("puts a microphone beside every free-text field of the capture form", async ({ page }) => {
  await page.goto("/app/p/_local");
  await page.getByRole("button", { name: "New task or issue" }).click();
  const dialog = page.getByRole("dialog", { name: "New task" });

  await expect(dialog.getByRole("button", { name: "Dictate into Title" })).toBeVisible();
  await expect(dialog.getByRole("button", { name: "Dictate into What happened" })).toBeVisible();

  // The specification fields are behind the disclosure, and hidden rather than
  // unmounted, so they are in the document either way -- assert on what a person can
  // actually reach.
  await dialog.getByRole("button", { name: "Add the full specification" }).click();
  for (const name of DICTATED) {
    await expect(dialog.getByRole("button", { name, exact: true })).toBeVisible();
  }
});

test("asks for the microphone only on a press, never on the way in", async ({ page }) => {
  // ac-7, asked of the browser rather than of the code: the permission is still
  // undecided after the form has rendered every one of its microphones.
  await page.goto("/app/p/_local");
  await page.getByRole("button", { name: "New task or issue" }).click();
  await expect(
    page.getByRole("dialog", { name: "New task" }).getByRole("button", {
      name: "Dictate into Title",
    }),
  ).toBeVisible();

  const state = await page.evaluate(() =>
    navigator.permissions.query({ name: "microphone" as PermissionName }).then((p) => p.state),
  );
  expect(state).toBe("prompt");
});

test("leaves the field an ordinary one, so the keyboard's own microphone still types", async ({
  page,
}) => {
  // The binding constraint from task-171: the operating system keyboard's microphone
  // is the only dictation path that works in every browser measured, and it stops
  // working the moment something cancels an input event or swaps the textarea for an
  // editor. Gboard's dictation arrived as `insertCompositionText` on the phone, so
  // that is the event asserted on -- a synthetic one, which shows only that nothing in
  // the application cancels it. Whether a real thumb on a real microphone key works is
  // ac-6, and a person has to answer that.
  await page.goto("/app/p/_local");
  await page.getByRole("button", { name: "New task or issue" }).click();
  const dialog = page.getByRole("dialog", { name: "New task" });
  const details = dialog.getByRole("textbox", { name: /^What happened/ });
  await details.fill("Typed by hand.");

  const cancelled = await details.evaluate((element) => {
    const event = new InputEvent("beforeinput", {
      inputType: "insertCompositionText",
      data: " Spoken.",
      bubbles: true,
      cancelable: true,
    });
    element.dispatchEvent(event);
    return event.defaultPrevented;
  });
  expect(cancelled).toBe(false);

  // And the prose is untouched by any of it.
  await expect(details).toHaveValue("Typed by hand.");
});
