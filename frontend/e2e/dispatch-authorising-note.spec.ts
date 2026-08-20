import { expect, test } from "@playwright/test";

/**
 * Task-185, through the real stack: a task an agent filed is refused a dispatch, and
 * the browser can now write the entry that authorises one.
 *
 * This is the shape of *every* task an agent files. Its only log entry is its own
 * creation transition, whose actor is an agent, so the human-clocked rule refuses it.
 * Before this work the refusal named a remedy no control on the page could perform, and
 * the task was un-dispatchable from a browser forever.
 *
 * The task is created over the API rather than through the Create page, because the
 * Create page files as the project's human and would produce a task that dispatches on
 * the first click — the very case that already worked. Everything after that is done
 * the way a person does it, and every assertion is on rendered text.
 */

const projectId = "_local";
const project = `/app/p/${projectId}`;
const taskId = "task-901-filed-by-an-agent";

test("refuses an agent-filed task, then dispatches it once a human writes a note", async ({
  page,
  request,
}) => {
  // Setup and teardown go through the API: the toggle has its own spec, and repeating
  // it here would only make this one slower at proving something already proven.
  const enabled = await request.post(`/api/projects/${projectId}/dispatch/enable`, {
    data: { runner: "e2e-sleeper" },
  });
  expect(enabled.ok()).toBeTruthy();

  const created = await request.post(`/api/projects/${projectId}/tasks`, {
    data: {
      id: taskId,
      actor: "claude",
      title: "Filed by an agent",
      summary: "Its only log entry is the creation transition an agent wrote.",
      description: "Proves the browser can author the entry a dispatch must trace to.",
      lifecycle: "ready",
    },
  });
  expect(created.ok(), await created.text()).toBeTruthy();

  await page.goto(`${project}/tasks/${taskId}`);

  // Exactly the state Jeff hit: the button is offered, and pressing it is refused.
  const dispatch = page.getByRole("region", { name: "Dispatch" });
  await dispatch.getByRole("button", { name: /dispatch/i }).click();

  const refusal = dispatch.getByRole("alert");
  await expect(refusal).toContainText("an agent");
  // The remedy names a control on this page, which is the whole defect. A refusal that
  // said "act on the task yourself" would satisfy a `toContainText` just as well and
  // leave the reader exactly as stuck.
  await expect(refusal).toContainText("Add a note");

  const notes = page.getByRole("region", { name: "Notes" });
  await notes.getByRole("button", { name: /add a note/i }).click();
  // Whose entry this will be, said before it is written rather than discovered after.
  await expect(notes).toContainText("Written as E2E Human");
  await notes.getByRole("textbox", { name: "Note" }).fill("Authorising this run.");
  await notes.getByRole("button", { name: "Save note" }).click();

  // Written to the record, as the human, and visible where the record is read.
  const log = page.getByRole("region", { name: "Task log" });
  await expect(log).toContainText("Authorising this run.");
  await expect(log.getByRole("article").first()).toContainText("E2E Human");

  await dispatch.getByRole("button", { name: /dispatch/i }).click();

  const run = dispatch.getByRole("listitem").first();
  await expect(run).toContainText("Running", { timeout: 15_000 });

  await run.getByRole("button", { name: /cancel run/i }).click();
  await expect(run).toContainText("Cancelled", { timeout: 15_000 });

  // Leave the shared project as it was found: these specs run in one server against one
  // project, and a cancelled run parks the ball with a human, where every later spec's
  // dashboard would report it as the project's next action.
  await page
    .getByRole("region", { name: "Review actions" })
    .getByRole("button", { name: /reject/i })
    .click();
  await page.getByLabel("Reason for rejection").fill("End-to-end run finished with it.");
  await page.getByRole("button", { name: "Submit" }).click();
  await expect(page).toHaveURL(new RegExp(`${project}/tasks$`));

  const disabled = await request.post(`/api/projects/${projectId}/dispatch/disable`);
  expect(disabled.ok()).toBeTruthy();
});
