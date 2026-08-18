import { expect, test } from "@playwright/test";

/**
 * Report Issue against the real server: the whole point of the action is that the
 * finding lands in the corpus, so the assertion has to be the stored record rather
 * than a form that submitted without complaining.
 */

test("files an issue from a task page and links the task the reporter was reading", async ({
  page,
}) => {
  // Something to be looking at when the finding happens.
  await page.goto("/app/");
  await page.getByRole("link", { name: "Create", exact: true }).click();
  await page.getByRole("textbox", { name: "Title", exact: true }).fill("Task under review");
  await page.getByRole("textbox", { name: /^Summary/ }).fill("A task to be reading when something is noticed.");
  await page.getByRole("textbox", { name: /^Working description/ }).fill("Nothing to do; it is the page the reporter is on.");
  await page.getByRole("button", { name: "Create task" }).click();
  await expect(page).toHaveURL(/\/tasks\?status=all$/);

  await page.getByRole("link", { name: /Task under review/ }).click();
  await expect(page.getByRole("region", { name: "Full specification" })).toBeVisible();
  const viewedTaskId = (await page.locator("div.select-all").first().innerText()).trim();

  await page.getByRole("button", { name: "Report issue" }).click();
  const dialog = page.getByRole("dialog", { name: "Report an issue" });
  await expect(dialog).toBeVisible();
  await dialog.getByRole("textbox", { name: /^Title/ }).fill("The log timestamps are unreadable");
  await dialog
    .getByRole("textbox", { name: /^What happened/ })
    .fill("Every entry shows a full locale string; on a phone it wraps to three lines.");
  await dialog.getByRole("button", { name: "File issue" }).click();

  await expect(dialog).toContainText("Filed as");
  await dialog.getByRole("link", { name: "Open the task" }).click();

  // The stored record, read the way a user reads it.
  await expect(page.getByRole("heading", { name: "The log timestamps are unreadable" })).toBeVisible();
  await expect(page.getByText("reported-issue")).toBeVisible();
  await expect(page.getByRole("region", { name: "Full specification" })).toContainText(
    `/p/_local/tasks/${viewedTaskId}`,
  );
  // Attributed to the configured human, not to "human".
  const log = page.getByRole("region", { name: "Task log" });
  await expect(log).toContainText("E2E Human");
  await expect(log).not.toContainText("Created draft by system");
  // Draft: a finding still needs someone to decide it is worth doing.
  await expect(page.getByRole("region", { name: "Dependency state" })).toContainText("Needs spec");
  // And the trail back to the page it was noticed on is followable, not just stored.
  const relationships = page.getByRole("region", { name: "Task relationships" });
  await expect(relationships).toContainText("Reported while viewing this task.");
  await relationships.getByRole("link", { name: viewedTaskId }).click();
  await expect(page.getByRole("heading", { name: "Task under review" })).toBeVisible();
});

test("is reachable from the project picker, where no project has resolved yet", async ({
  page,
  request,
}) => {
  // The route that renders before any project is in scope. The reporter has to work
  // here too, because a finding about the picker itself has nowhere else to go.
  await page.goto("/app/not-found");
  await page.getByRole("button", { name: "Report issue" }).click();

  const dialog = page.getByRole("dialog", { name: "Report an issue" });
  await dialog.getByRole("textbox", { name: /^Title/ }).fill("Not-found page has no way back");
  await dialog.getByRole("textbox", { name: /^What happened/ }).fill("Reported from a page with no project.");
  await expect(dialog.getByRole("combobox", { name: "File into project" })).toBeVisible();
  await dialog.getByRole("button", { name: "File issue" }).click();
  await expect(dialog).toContainText("Filed as");

  const filed = (await dialog.getByRole("status").innerText()).replace(/^Filed as\s*/, "").replace(/\.$/, "");
  const record = await (await request.get(`/api/tasks/${filed}`)).json();
  expect(record.tags).toEqual(["reported-issue"]);
  expect(record.dependencies).toEqual([]);
  expect(record.spec.description).toContain("/not-found");
  expect(record.log[0].actor).toBe("E2E Human");
});
