import { expect, test } from "@playwright/test";

test("creates a ready task through the real stack and shows it in the list", async ({ page }) => {
  await page.goto("/app/");
  // The nav link, not the dashboard's "Create task" call-to-action. That one renders
  // only while the dashboard is empty, so reaching it silently required this file to
  // run before every other spec -- an ordering nothing enforced, and which a new spec
  // file sorting ahead of this one duly broke. The entry point is incidental here;
  // what this test is for is the create -> API -> list round trip.
  await page.getByRole("link", { name: "Create", exact: true }).click();

  await page.getByRole("textbox", { name: "Title", exact: true }).fill("Playwright-created task");
  await page.getByRole("textbox", { name: /^Summary/ }).fill("Proves server, API, generated client, and browser agree.");
  await page.getByRole("textbox", { name: /^Working description/ }).fill("Create this record in the temporary end-to-end project.");
  await page.getByRole("radio", { name: /^Ready/ }).check();
  await page.getByRole("button", { name: "Create task" }).click();

  await expect(page).toHaveURL(/\/app\/p\/_local\/tasks\?status=all$/);
  const tasks = page.getByRole("region", { name: "Tasks" });
  await expect(tasks.getByText("task-001")).toBeVisible();
  await expect(tasks.getByText("Playwright-created task")).toBeVisible();
  await expect(tasks.getByText("Actionable now")).toBeVisible();
});
