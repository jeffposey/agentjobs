import { expect, test, type APIRequestContext } from "@playwright/test";

/**
 * The three things that made reviewing a finished task hard, exercised end to end:
 * searching by the number people actually quote, reading a long log without opening
 * every entry by hand, and being told what a closed task's outcome really was.
 *
 * Against the real server, because each one crosses a boundary the component tests
 * stub: the id search is storage's, the outcome label is the backend's display_status,
 * and "browser find works" is only true of a real <details> in a real browser.
 */

/** The digits in `task-042`, which is what a reviewer types into the search box. */
function numberOf(taskId: string) {
  return taskId.split("-")[1];
}

async function createTask(request: APIRequestContext, title: string) {
  const response = await request.post("/api/tasks", {
    data: {
      title,
      summary: "Created for the review-findability path.",
      description: "Nothing in this record repeats the task number in prose.",
      lifecycle: "ready",
      category: "ux",
    },
  });
  expect(response.ok()).toBeTruthy();
  return (await response.json()).id as string;
}

test("finds a task by the number a reviewer was quoted, not just by its title", async ({ page, request }) => {
  const wanted = await createTask(request, "Multi project GUI");
  const other = await createTask(request, "Something else entirely");

  await page.goto("/app/p/_local/tasks?status=all");
  const tasks = page.getByRole("region", { name: "Tasks" });
  await expect(tasks.getByText(wanted, { exact: true })).toBeVisible();

  // The bare digits, exactly as they appear in "have a look at 058".
  await page.getByRole("searchbox", { name: "Search tasks" }).fill(numberOf(wanted));

  await expect(tasks.getByText(wanted, { exact: true })).toBeVisible();
  await expect(tasks.getByText(other, { exact: true })).toBeHidden();

  // The API answers the same question the same way, so a client that is not this
  // list gets the task too.
  const found = await (await request.get(`/api/search?q=${numberOf(wanted)}`)).json();
  expect(found.map((task: { id: string }) => task.id)).toContain(wanted);
});

test("says what a closed task's outcome actually was", async ({ page, request }) => {
  const superseded = await createTask(request, "Replaced by a later design");
  const completed = await createTask(request, "Finished as intended");
  for (const [taskId, outcome] of [[superseded, "superseded"], [completed, "completed"]] as const) {
    const response = await request.post(`/api/tasks/${taskId}/close`, {
      data: { actor: "E2E Human", outcome, body: `Closed ${outcome} for the browser path.` },
    });
    expect(response.ok()).toBeTruthy();
  }

  await page.goto("/app/p/_local/tasks?status=closed");
  const tasks = page.getByRole("region", { name: "Tasks" });
  await expect(tasks).toContainText("Superseded");
  await expect(tasks).toContainText("Completed");
  // The label that hid the difference. Both rows said it, so the list claimed a
  // superseded task had been finished.
  await expect(tasks.getByText("Done", { exact: true })).toHaveCount(0);

  await page.goto(`/app/p/_local/tasks/${superseded}`);
  await expect(page.getByRole("region", { name: "Dependency state" })).toContainText("Superseded");
});

test("opens every collapsed log entry with one control so the page can be searched", async ({ page, request }) => {
  const taskId = await createTask(request, "A task with a long history");
  const marker = "sentinel-phrase-only-inside-a-collapsed-entry";
  // Long enough to be collapsed by default, and not the newest entry, which stays open.
  const body = `${marker} ${"filler ".repeat(80)}`;
  for (const entry of [body, "A short newest entry."]) {
    const response = await request.post(`/api/tasks/${taskId}/log`, {
      data: { actor: "E2E Human", type: "progress", body: entry },
    });
    expect(response.ok()).toBeTruthy();
  }

  await page.goto(`/app/p/_local/tasks/${taskId}`);
  const log = page.getByRole("region", { name: "Task log" });
  const collapsed = log.locator("details", { hasText: marker });
  // Present in the DOM but closed, which is what defeats Ctrl+F and select-all.
  await expect(collapsed).toHaveJSProperty("open", false);

  await log.getByRole("button", { name: "Expand all entries" }).click();

  await expect(collapsed).toHaveJSProperty("open", true);
  await expect(log.getByText(marker)).toBeVisible();
  // Every entry, not only the one that was asked about.
  expect(await log.locator("details[open]").count()).toEqual(await log.locator("details").count());

  await log.getByRole("button", { name: "Collapse long entries" }).click();
  await expect(collapsed).toHaveJSProperty("open", false);
});
