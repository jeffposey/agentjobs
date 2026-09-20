import { expect, test } from "@playwright/test";

/**
 * Filing a finding against the real server, from the one capture control.
 *
 * The whole point of the action is that the finding lands in the corpus, so the
 * assertion has to be the stored record rather than a form that submitted without
 * complaining. Since task-346 the entry point is the header's capture trigger rather
 * than a floating button, and the record it produces is the thing that must not have
 * changed.
 */

test("files an issue from a task page and links the task the reporter was reading", async ({
  page,
}) => {
  // Something to be looking at when the finding happens.
  await page.goto("/app/p/_local/tasks/new");
  await page.getByRole("textbox", { name: "Title", exact: true }).fill("Task under review");
  await page.getByRole("textbox", { name: /^Summary/ }).fill("A task to be reading when something is noticed.");
  await page.getByRole("textbox", { name: /^What happened/ }).fill("Nothing to do; it is the page the reporter is on.");
  await page.getByRole("button", { name: "File it" }).click();
  await expect(page).toHaveURL(/\/tasks\?status=all$/);

  await page.getByRole("link", { name: /Task under review/ }).click();
  await expect(page.getByRole("region", { name: "Full specification" })).toBeVisible();
  const viewedTaskId = (await page.locator("div.select-all").first().innerText()).trim();

  await page.getByRole("button", { name: "New task or issue" }).click();
  const dialog = page.getByRole("dialog", { name: "New task" });
  await expect(dialog).toBeVisible();
  await dialog.getByRole("textbox", { name: /^Title/ }).fill("The log timestamps are unreadable");
  await dialog
    .getByRole("textbox", { name: /^What happened/ })
    .fill("Every entry shows a full locale string; on a phone it wraps to three lines.");
  await dialog.getByRole("button", { name: "File it" }).click();

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

test("is reachable from a page where no project has resolved yet", async ({ page, request }) => {
  // This route renders before any project is in scope, so there is no header and the
  // control is the one mounted beside the routes. A finding about the project picker
  // has nowhere else to go, and losing that surface is what moving into the header had
  // to avoid.
  await page.goto("/app/not-found");
  await page.getByRole("button", { name: "New task or issue" }).click();

  const dialog = page.getByRole("dialog", { name: "New task" });
  await dialog.getByRole("textbox", { name: /^Title/ }).fill("Not-found page has no way back");
  await dialog.getByRole("textbox", { name: /^What happened/ }).fill("Reported from a page with no project.");
  await expect(dialog.getByRole("combobox", { name: "File into project" })).toBeVisible();
  await dialog.getByRole("button", { name: "File it" }).click();
  await expect(dialog).toContainText("Filed as");

  const filed = (await dialog.getByRole("status").innerText()).replace(/^Filed as\s*/, "").replace(/\.$/, "");
  const record = await (await request.get(`/api/tasks/${filed}`)).json();
  expect(record.tags).toEqual(["reported-issue"]);
  expect(record.dependencies).toEqual([]);
  expect(record.spec.description).toContain("/not-found");
  expect(record.log[0].actor).toBe("E2E Human");
});

test("offers exactly one capture control, and nothing in the bottom-right corner", async ({
  page,
}) => {
  // The control is mounted twice -- in the header, and beside the routes for the pages
  // that have no header -- and the second must render nothing where the first already
  // is. Two triggers in one corner would be this task's own duplication, reintroduced
  // by its fix for coverage.
  await page.goto("/app/p/_local");
  await expect(page.getByRole("button", { name: "New task or issue" })).toHaveCount(1);

  // And nothing hovers over the bottom-right, where iOS puts the home indicator
  // (task-295). Asked of the browser's computed styles rather than of a class name.
  const floating = await page.evaluate(
    () =>
      [...document.querySelectorAll("button")].filter((button) => {
        const style = getComputedStyle(button);
        return style.position === "fixed" && style.bottom !== "auto" && style.right !== "auto";
      }).length,
  );
  expect(floating).toBe(0);
});

test("stays one tap away at 375px, where the burger takes the destinations", async ({ page }) => {
  // The narrowest phone this app is read on. Every destination is behind the burger at
  // this width, and the whole argument for moving capture into the bar is that it is
  // *not* one of them -- so it has to still be on screen, and still be a real target.
  await page.setViewportSize({ width: 375, height: 812 });
  await page.goto("/app/p/_local");

  const trigger = page.getByRole("button", { name: "New task or issue" });
  await expect(trigger).toBeVisible();
  const box = (await trigger.boundingBox())!;
  expect(box.height).toBeGreaterThanOrEqual(44);
  expect(box.width).toBeGreaterThanOrEqual(44);
  expect(box.x + box.width).toBeLessThanOrEqual(375);

  // Opening it must not grow or move the bar underneath it.
  const headerHeight = () =>
    page.evaluate(() => document.querySelector("header")!.getBoundingClientRect().height);
  const before = await headerHeight();
  await trigger.click();
  await expect(page.getByRole("dialog", { name: "New task" })).toBeVisible();
  expect(await headerHeight()).toBe(before);
});
