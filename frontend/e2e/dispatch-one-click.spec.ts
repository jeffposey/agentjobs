import { expect, test } from "@playwright/test";

/**
 * Task-188, through the real stack: a task an agent filed dispatches on one click.
 *
 * This is the shape of *every* task an agent files. Its only log entry is its own
 * creation transition, whose actor is an agent — which is why, until 2026-08-20, the
 * human-clocked rule refused 72 of this project's 74 open tasks and the human had to
 * know to write a note first. The server now writes the authorising entry itself,
 * attributed to whoever is signed in, and the record is the brief.
 *
 * These tasks are created over the API rather than through the Create page, because the
 * Create page files as the project's human and would produce a task whose newest entry
 * is already a human's — the one case that always worked, which would prove nothing.
 * Everything after that is done the way a person does it, and every assertion is on
 * rendered text.
 */

const projectId = "_local";
const project = `/app/p/${projectId}`;

test("dispatches an agent-filed task on the first click, and puts the authorisation on the record", async ({
  page,
  request,
}) => {
  const taskId = "task-901-filed-by-an-agent";
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
      description: "Proves a complete record is its own brief, and needs no ceremony.",
      lifecycle: "ready",
    },
  });
  expect(created.ok(), await created.text()).toBeTruthy();

  await page.goto(`${project}/tasks/${taskId}`);

  const dispatch = page.getByRole("region", { name: "Dispatch" });
  // Whose name goes on the run, said before the click rather than discovered after.
  await expect(dispatch).toContainText("authorised by");
  await expect(dispatch).toContainText("E2E Human");
  // No box, because the record can brief an agent. The ask is the special occasion.
  await expect(dispatch.getByRole("textbox")).toHaveCount(0);

  await dispatch.getByRole("button", { name: /dispatch/i }).click();

  const run = dispatch.getByRole("listitem").first();
  await expect(run).toContainText("Running", { timeout: 15_000 });

  // The authorising entry is a real row in the log, under the human's name, written
  // without anyone typing it. Asserted where the record is read, not in the response.
  const log = page.getByRole("region", { name: "Task log" });
  await expect(log).toContainText("Dispatched by E2E Human from the task page");
  await expect(log).toContainText("the task record is the brief");

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

test("asks for text only when the record cannot brief an agent", async ({ page, request }) => {
  const taskId = "task-902-nothing-to-go-on";
  const enabled = await request.post(`/api/projects/${projectId}/dispatch/enable`, {
    data: { runner: "e2e-sleeper" },
  });
  expect(enabled.ok()).toBeTruthy();

  const created = await request.post(`/api/projects/${projectId}/tasks`, {
    data: {
      id: taskId,
      actor: "claude",
      title: "Nothing to go on",
      summary: "Filed with no working specification.",
      description: "",
      lifecycle: "ready",
    },
  });
  expect(created.ok(), await created.text()).toBeTruthy();

  await page.goto(`${project}/tasks/${taskId}`);

  // No run is started here. What this proves is that the trigger fires on the record's
  // own emptiness, and that the button will not start anything until it is answered.
  // That the typed text becomes the authorising entry is pinned at the API level, where
  // the entry can be read back by id instead of matched in rendered prose.
  const dispatch = page.getByRole("region", { name: "Dispatch" });
  await expect(dispatch).toContainText("no specification");
  await expect(dispatch.getByRole("textbox")).toHaveCount(1);
  await expect(dispatch.getByRole("button", { name: /dispatch/i })).toBeDisabled();

  await dispatch.getByRole("textbox").fill("Port the widget to v2.");
  await expect(dispatch.getByRole("button", { name: /dispatch/i })).toBeEnabled();

  // Nothing was started, so there is no run to cancel and no ball parked with a human.
  // Archived over the API rather than rejected through the panel: a ready task in the
  // pool has no review to act on, and these specs share one project.
  const archived = await request.delete(`/api/projects/${projectId}/tasks/${taskId}`);
  expect(archived.ok(), await archived.text()).toBeTruthy();

  const disabled = await request.post(`/api/projects/${projectId}/dispatch/disable`);
  expect(disabled.ok()).toBeTruthy();
});
