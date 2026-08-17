import { expect, test, type Page } from "@playwright/test";

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
 * Open a task's detail page the way a user does: by clicking it in the task list.
 *
 * This used to look the id up over the API and navigate directly, because the row
 * link was a raw anchor that dropped the /app basename and ejected the browser into
 * the legacy server-rendered UI (task-008, fixed). Clicking is worth more than the
 * workaround was: it means these tests also fail if that regresses.
 */
async function openTask(page: Page, title: string) {
  await page.goto("/app/p/_local/tasks?status=all");
  await page.getByRole("link", { name: new RegExp(title) }).click();
  await expect(page.getByRole("region", { name: "Full specification" })).toBeVisible();
  // Still inside the React app, not the Jinja page the old anchor landed on.
  await expect(page).toHaveURL(/\/app\/p\/_local\/tasks\//);
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

  const panel = page.getByRole("region", { name: "Draft actions" });
  await expect(panel).toBeVisible();
  await expect(page.getByRole("region", { name: "Dependency state" })).toContainText("Needs spec");

  await panel.getByRole("button", { name: /Promote — make it claimable/ }).click();
  await panel.getByLabel("Promotion note (optional)").fill("Spec is finished; open for claiming.");
  await panel.getByRole("button", { name: "Promote", exact: true }).click();

  // Gone, because the ball has left the human -- and the state a human reads is
  // the promoted one, not merely different markup.
  await expect(page.getByRole("region", { name: "Draft actions" })).toBeHidden();
  await expect(page.getByRole("region", { name: "Dependency state" })).toContainText("Actionable now");

  const log = page.getByRole("region", { name: "Task log" });
  await expect(log).toContainText("Spec is finished; open for claiming.");
  await expect(log).toContainText("E2E Human");
});

test("wears the review vocabulary once a task is past draft", async ({ page }) => {
  await createTask(page, "Ready from the start", "Ready");
  await openTask(page, "Ready from the start");

  // Ready/agent-available: the ball is not with the human, so no action panel at all.
  await expect(page.getByRole("region", { name: "Draft actions" })).toBeHidden();
  await expect(page.getByRole("region", { name: "Review actions" })).toBeHidden();
  await expect(page.getByRole("region", { name: "Dependency state" })).toContainText("Actionable now");
});

test("promoting without a note records the manager's own sentence", async ({ page }) => {
  await createTask(page, "Draft without a note", "Draft");
  await openTask(page, "Draft without a note");

  await page.getByRole("button", { name: /Promote — make it claimable/ }).click();
  await page.getByRole("button", { name: "Promote", exact: true }).click();

  await expect(page.getByRole("region", { name: "Draft actions" })).toBeHidden();
  await expect(page.getByRole("region", { name: "Task log" })).toContainText(
    "Promoted by E2E Human; the spec is finished and it is claimable.",
  );
});

test("a task changed underneath the open page is refused, reloaded and re-offered", async ({ page, request }) => {
  await createTask(page, "Draft changed underneath", "Draft");
  await openTask(page, "Draft changed underneath");

  const panel = page.getByRole("region", { name: "Draft actions" });
  await expect(panel).toBeVisible();

  // Move the task from another surface while this page still holds the revision it
  // loaded with.
  const taskId = (await page.locator("div.select-all").first().innerText()).trim();
  const elsewhere = await request.post(`/api/tasks/${taskId}/promote`, {
    data: { actor: "codex", body: "Promoted from another surface." },
  });
  expect(elsewhere.ok()).toBeTruthy();

  await panel.getByRole("button", { name: /Promote — make it claimable/ }).click();
  await panel.getByRole("button", { name: "Promote", exact: true }).click();

  // Refused, explained, and the record re-read: not written twice, and not silent.
  await expect(page.getByRole("alert")).toContainText("changed while the page was open");
  const log = page.getByRole("region", { name: "Task log" });
  await expect(log).toContainText("Promoted from another surface.");
  await expect(log.getByRole("article")).toHaveCount(1);
});

test("send feedback and reject still work on a draft, unchanged", async ({ page, request }) => {
  // The relabelling is cosmetic for these two: they must still call request-changes
  // and reject, and land the same records they always did.
  await createTask(page, "Draft that gets feedback", "Draft");
  await openTask(page, "Draft that gets feedback");

  const panel = page.getByRole("region", { name: "Draft actions" });
  await panel.getByRole("button", { name: /Send feedback/ }).click();
  await panel.getByLabel("Feedback on the spec").fill("The acceptance criteria are not testable yet.");
  await panel.getByRole("button", { name: "Submit" }).click();

  // request-changes hands the ball back to the agent to revise, and the feedback
  // rides in the ball_prompt verbatim.
  await expect(page.getByRole("region", { name: "Task log" })).toContainText(
    "The acceptance criteria are not testable yet.",
  );
  // Still a draft, now with the agent, reason revise -- which is exactly what
  // feedback on a spec should mean: go rewrite it, it is not ready yet. And the
  // panel is gone, because the ball is no longer with the human.
  await expect(page.getByRole("region", { name: "Dependency state" })).toContainText("Revising");
  await expect(page.getByRole("region", { name: "Draft actions" })).toBeHidden();

  await createTask(page, "Draft that gets rejected", "Draft");
  await openTask(page, "Draft that gets rejected");
  const rejectPanel = page.getByRole("region", { name: "Draft actions" });
  const taskId = (await page.locator("div.select-all").first().innerText()).trim();
  await rejectPanel.getByRole("button", { name: /Reject & Archive/ }).click();
  await rejectPanel.getByLabel("Reason for rejection").fill("Duplicate of an existing draft.");
  await rejectPanel.getByRole("button", { name: "Submit" }).click();

  // reject closes the task cancelled and archives it, so the list no longer carries it.
  await expect(page).toHaveURL(/\/tasks$/);
  const record = await (await request.get(`/api/tasks/${taskId}`)).json();
  expect(record.lifecycle).toBe("closed");
  expect(record.outcome).toBe("cancelled");
});
