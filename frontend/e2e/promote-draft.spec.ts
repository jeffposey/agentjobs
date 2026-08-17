import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

/**
 * The loop the dashboard already promises: a human writes a draft, the drafts panel
 * says it needs a decision before it becomes work, and this is where that decision
 * gets made. Run against the real server, so one assertion covers the endpoint, the
 * generated client and the rendered page together.
 */

async function createTask(page: Page, title: string, lifecycle: "Draft" | "Ready") {
  await page.goto("/app/");
  // The nav link, not the dashboard's call-to-action button: that one renders only
  // for some dashboard states, and creating drafts moves the dashboard out of them.
  await page.getByRole("link", { name: "Create", exact: true }).click();
  await page.getByRole("textbox", { name: "Title", exact: true }).fill(title);
  await page.getByRole("textbox", { name: /^Summary/ }).fill("A task written from the UI.");
  await page.getByRole("textbox", { name: /^Working description/ }).fill("Exercising the promote control.");
  await page.getByRole("radio", { name: new RegExp(`^${lifecycle}`) }).check();
  await page.getByRole("button", { name: "Create task" }).click();
  await expect(page).toHaveURL(/\/tasks\?status=all$/);
}

/**
 * Open a task's detail page by id.
 *
 * Deliberately not by clicking it in the task list: `TaskList.tsx` renders a raw
 * `<a href>` holding a router path with no `/app` basename, so that click leaves the
 * React app for the legacy server-rendered page. That is a real bug and a separate
 * one; routing around it here keeps these tests about promote.
 */
async function openTask(page: Page, request: APIRequestContext, title: string) {
  const tasks = await (await request.get("/api/tasks")).json();
  const match = tasks.find((task: { title: string }) => task.title === title);
  expect(match, `no task titled ${title}`).toBeTruthy();
  await page.goto(`/app/p/_local/tasks/${match.id}`);
  await expect(page.getByRole("region", { name: "Full specification" })).toBeVisible();
}

test("walks the whole drafts loop: create, find through the dashboard, promote", async ({ page }) => {
  await createTask(page, "Draft to promote", "Draft");

  // Find it the way the dashboard invites: the drafts panel that says these need a
  // decision before they become work.
  await page.goto("/app/");
  const backlog = page.getByRole("table", { name: "Backlog awaiting your input" });
  await expect(backlog).toContainText("Draft to promote");
  // The drafts table links the task id and prints the title in a plain cell, so the
  // row is what identifies the draft to a reader and the link inside it is the way in.
  await backlog.getByRole("row", { name: /Draft to promote/ }).getByRole("link").click();

  const panel = page.getByRole("region", { name: "Promote draft" });
  await expect(panel).toBeVisible();
  await expect(page.getByRole("region", { name: "Dependency state" })).toContainText("Needs spec");

  await panel.getByLabel("Promotion note (optional)").fill("Spec is finished; open for claiming.");
  await panel.getByRole("button", { name: /Promote to Ready/ }).click();

  // Gone, because the task is no longer a draft -- and the state a human reads is
  // the promoted one, not merely different markup.
  await expect(page.getByRole("region", { name: "Promote draft" })).toBeHidden();
  await expect(page.getByRole("region", { name: "Dependency state" })).toContainText("Actionable now");

  const log = page.getByRole("region", { name: "Task log" });
  await expect(log).toContainText("Spec is finished; open for claiming.");
  await expect(log).toContainText("E2E Human");
});

test("offers no promote control on a task that is not a draft", async ({ page, request }) => {
  await createTask(page, "Ready from the start", "Ready");
  await openTask(page, request, "Ready from the start");

  await expect(page.getByRole("region", { name: "Promote draft" })).toBeHidden();
  await expect(page.getByRole("region", { name: "Dependency state" })).toContainText("Actionable now");
});

test("promoting without a note records the manager's own sentence", async ({ page, request }) => {
  await createTask(page, "Draft without a note", "Draft");
  await openTask(page, request, "Draft without a note");

  await page.getByRole("button", { name: /Promote to Ready/ }).click();

  await expect(page.getByRole("region", { name: "Promote draft" })).toBeHidden();
  await expect(page.getByRole("region", { name: "Task log" })).toContainText(
    "Promoted by E2E Human; the spec is finished and it is claimable.",
  );
});

test("a task changed underneath the open page is refused, reloaded and re-offered", async ({ page, request }) => {
  await createTask(page, "Draft changed underneath", "Draft");
  await openTask(page, request, "Draft changed underneath");

  const panel = page.getByRole("region", { name: "Promote draft" });
  await expect(panel).toBeVisible();

  // Move the task from another surface while this page still holds the revision it
  // loaded with.
  const taskId = (await page.locator("div.select-all").first().innerText()).trim();
  const elsewhere = await request.post(`/api/tasks/${taskId}/promote`, {
    data: { actor: "codex", body: "Promoted from another surface." },
  });
  expect(elsewhere.ok()).toBeTruthy();

  await panel.getByRole("button", { name: /Promote to Ready/ }).click();

  // Refused, explained, and the record re-read: not written twice, and not silent.
  await expect(page.getByRole("alert")).toContainText("changed while the page was open");
  const log = page.getByRole("region", { name: "Task log" });
  await expect(log).toContainText("Promoted from another surface.");
  await expect(log.getByRole("article")).toHaveCount(1);
});
