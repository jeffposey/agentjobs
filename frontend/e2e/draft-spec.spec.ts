import { expect, test } from "./fixtures";

/**
 * Drafting a spec through the whole stack: browser, generated client, route, provider.
 *
 * The provider is a stub on loopback that `e2e/run_server.py` starts and points
 * `model.yaml` at, so this path runs on a machine with no credential and spends nothing.
 * What it proves is the plumbing -- that a press reaches a provider and its answer lands
 * in the form fields a person then files. Whether a real model writes a good spec is a
 * different question, needs a real one, and is recorded as still open in
 * `docs/model-access-design.md` section 9.
 */

const DRAFTED_SUMMARY = "The task list pages badly once a project holds a few hundred tasks.";

test("drafts a spec into the create form, and files what the person approves", async ({ page }) => {
  // The create page directly. It is no longer a nav destination -- task-346 replaced
  // that link with the header's capture control -- and this spec is not about how you
  // reach the form.
  await page.goto("/app/p/_local/tasks/new");

  const draftCheckbox = page.getByRole("checkbox", { name: /Flesh this out with AI/ });
  await expect(draftCheckbox).toBeEnabled();
  await expect(draftCheckbox).toBeChecked();

  await page.getByRole("textbox", { name: "Title", exact: true }).fill("Paging is slow");
  const summary = page.getByRole("textbox", { name: /^Summary/ });
  await summary.fill("The one true sentence I dictated.");
  await page
    .getByRole("textbox", { name: /^What happened/ })
    .fill("it drags once a project has a few hundred tasks");

  await page.getByRole("button", { name: "Draft the spec" }).click();

  // The draft lands in the fields, and the banner says which of my own text it took.
  await expect(summary).toHaveValue(DRAFTED_SUMMARY);
  await expect(page.getByText(/Replaced what you had written in Summary/)).toBeVisible();
  await expect(page.getByRole("textbox", { name: "Intent" })).toHaveValue(
    /Filing more work should not make the backlog harder to read/,
  );
  await expect(page.getByRole("textbox", { name: /^Acceptance criteria/ })).toHaveValue(
    /renders its first page without loading all of them/,
  );

  // Reversible: my sentence comes back exactly.
  await page.getByRole("button", { name: "Undo the draft" }).click();
  await expect(summary).toHaveValue("The one true sentence I dictated.");

  // And draftable again, then edited by hand before anything is filed. Nothing has been
  // saved at any point above: this is the first request that writes a record.
  await page.getByRole("button", { name: "Draft the spec" }).click();
  await expect(summary).toHaveValue(DRAFTED_SUMMARY);
  await summary.fill(`${DRAFTED_SUMMARY} And a sentence I added myself.`);
  await page.getByRole("checkbox", { name: /^Ready for an agent/ }).check();
  await page.getByRole("button", { name: "File it" }).click();

  await expect(page).toHaveURL(/\/app\/p\/_local\/tasks\?status=all$/);
  await page.getByRole("region", { name: "Tasks" }).getByText("Paging is slow").click();

  // The filed record carries what the person approved, edit included, and is an
  // ordinary task: nothing on the page marks it as having been drafted.
  await expect(page.getByText("And a sentence I added myself.")).toBeVisible();
  await expect(page.getByText(/Filing more work should not make the backlog harder/)).toBeVisible();
});
