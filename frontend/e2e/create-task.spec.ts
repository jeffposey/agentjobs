import { expect, test } from "@playwright/test";

test("creates a ready task through the real stack and shows it in the list", async ({ page }) => {
  await page.goto("/app/");
  await page.getByRole("link", { name: "Create task" }).click();

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
