import { expect, test, type Page } from "./fixtures";

/**
 * The loop the dashboard already promises: a human writes a draft, the dashboard's
 * backlog count leads to it, and this is where the decision that makes it work gets
 * made. Run against the real server, so one assertion covers the endpoint, the
 * generated client and the rendered page together.
 */

async function createTask(page: Page, title: string, lifecycle: "Draft" | "Ready") {
  // The create page directly. It is no longer a nav destination -- task-346 replaced
  // that link with the header's capture control -- and this spec is not about how you
  // reach the form.
  await page.goto("/app/p/_local/tasks/new");
  await page.getByRole("textbox", { name: "Title", exact: true }).fill(title);
  await page.getByRole("textbox", { name: /^Summary/ }).fill("A task written from the UI.");
  await page.getByRole("textbox", { name: /^What happened/ }).fill("Exercising the promote control.");
  // One checkbox since task-346, not a pair of radios: off is draft, on is ready.
  const ready = page.getByRole("checkbox", { name: /^Ready for an agent/ });
  if (lifecycle === "Ready") await ready.check();
  await page.getByRole("button", { name: "File it" }).click();
  await expect(page).toHaveURL(/\/tasks\?status=all$/);
}

/** The Dashboard's "Active tasks" card, found by the heading a reader sees on it. */
function activeTasks(page: Page) {
  return page
    .locator("section")
    .filter({ has: page.getByRole("heading", { name: /^Active tasks/ }) });
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

  // Find it the way the dashboard invites. Not the drafts *panel*: since task-337 that
  // renders only when there is nothing claimable to offer instead, so whether it is on
  // screen depends on what ran before this on the same worker.
  //
  // The unconditional route used to be the "+N in backlog" link under the statistics
  // card. task-294 took that card off the Dashboard, and this is the route that
  // replaced it: the Active tasks section's "View all" link, which is unconditional for
  // the same reason, and then the Tasks surface's own Status filter. Three clicks rather
  // than one since task-356 put that filter behind a button, and every step of it is a
  // control a person can see.
  //
  // Scoped to that section, because the drafts panel's own link also begins "View all"
  // and the two are ambiguous whenever both are on screen. Until task-369 that was rare
  // enough to look like it could not happen: every spec shared one project, and by the
  // time this one ran there was always something claimable in it.
  await page.goto("/app/");
  await activeTasks(page).getByRole("link", { name: /^View all/ }).click();
  await expect(page).toHaveURL(/\/tasks$/);
  await page.getByRole("button", { name: /^Filters/ }).click();
  await page.getByLabel("Status").selectOption("draft");
  await expect(page).toHaveURL(/\/tasks\?status=draft$/);
  // The popover stays open across a change, so two filters are one visit rather than
  // two; it sits over the top of the list while it is, and dismissing it is the gesture
  // a person makes next. Escape rather than a click on the row underneath -- clicking
  // through a popover is not a thing a person can do either.
  await page.keyboard.press("Escape");
  await page.getByRole("link", { name: /Draft to promote/ }).click();

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
  // Exactly one promotion, not two. Counted by what the entries say rather than by how
  // many there are: a created task also carries a creation entry naming its author, so
  // a bare count would move every time anything else is recorded at creation.
  await expect(log.getByRole("article").filter({ hasText: "Promoted" })).toHaveCount(1);
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
