import { expect, test } from "@playwright/test";

/**
 * Authoring a whole task from the capture control, through the real stack.
 *
 * task-346 merged Create and Report issue into one control over one form, so this is
 * the half that has to prove nothing was lost on the way: every field `/tasks/new`
 * collected is still reachable, in the dialog, without leaving the page you were on.
 */

test("authors a full task from the capture control and stores every field", async ({
  page,
  request,
}) => {
  // The resolved project, not "/app/", which renders a redirect card first: the control
  // is mounted there too (that is the point of the global mount), and clicking it on
  // the way through opens a dialog the redirect then unmounts.
  await page.goto("/app/p/_local");
  await page.getByRole("button", { name: "New task or issue" }).click();
  const dialog = page.getByRole("dialog", { name: "New task" });

  await dialog.getByRole("textbox", { name: /^Title/ }).fill("Playwright-created task");
  await dialog
    .getByRole("textbox", { name: /^What happened/ })
    .fill("Create this record in the temporary end-to-end project.");
  await dialog.getByRole("checkbox", { name: /^Ready for an agent/ }).check();

  // One control, and everything the create page ever offered is under it.
  await dialog.getByRole("button", { name: "Add the full specification" }).click();
  await dialog
    .getByRole("textbox", { name: /^Summary/ })
    .fill("Proves server, API, generated client, and browser agree.");
  await dialog.getByRole("textbox", { name: "Intent" }).fill("Why this task exists.");
  await dialog.getByRole("textbox", { name: "Constraints" }).fill("Must not need a terminal.");
  await dialog.getByRole("textbox", { name: /^Out of scope/ }).fill("Everything else.");
  await dialog
    .getByRole("textbox", { name: /^Read-first context/ })
    .fill("src/agentjobs/manager.py | Owns creation");
  await dialog
    .getByRole("textbox", { name: /^Acceptance criteria/ })
    .fill("It appears in the task list\nIt starts ready");
  // task-342: the priority a report could never set.
  await dialog.getByRole("combobox", { name: "Priority" }).selectOption("high");
  await dialog.getByRole("textbox", { name: "Category" }).fill("ux");
  await dialog.getByRole("textbox", { name: "Effort" }).fill("Half a day");
  await dialog.getByRole("textbox", { name: /^Tags/ }).fill("gui, testing");

  await dialog.getByRole("button", { name: "File it" }).click();
  await expect(dialog).toContainText("Filed as");
  // Read off the attribute rather than parsed out of the sentence: since task-176 the
  // prose around the id changes with what happened to the dispatch, and the id does not.
  const filed = (await dialog.getByRole("status").getAttribute("data-task-id")) ?? "";

  // The stored record, asked of the server rather than read back off the form.
  const record = await (await request.get(`/api/tasks/${filed}`)).json();
  expect(record.title).toBe("Playwright-created task");
  expect(record.lifecycle).toBe("ready");
  expect(record.priority).toBe("high");
  expect(record.category).toBe("ux");
  expect(record.effort).toBe("Half a day");
  expect(record.tags).toEqual(["gui", "testing"]);
  expect(record.spec.summary).toBe("Proves server, API, generated client, and browser agree.");
  expect(record.spec.intent).toBe("Why this task exists.");
  expect(record.spec.constraints).toBe("Must not need a terminal.");
  expect(record.spec.out_of_scope).toBe("Everything else.");
  expect(record.spec.context).toEqual([
    { path: "src/agentjobs/manager.py", why: "Owns creation" },
  ]);
  expect(record.acceptance.map((entry: { text: string }) => entry.text)).toEqual([
    "It appears in the task list",
    "It starts ready",
  ]);

  // And it is in the list, which is the round trip the old spec was for.
  await dialog.getByRole("link", { name: /^Open task-/ }).click();
  await expect(page.getByRole("heading", { name: "Playwright-created task" })).toBeVisible();
  await page.goto("/app/p/_local/tasks?status=all");
  const tasks = page.getByRole("region", { name: "Tasks" });
  await expect(tasks.getByText("Playwright-created task")).toBeVisible();
  await expect(tasks.getByText("Actionable now")).toBeVisible();
});

test("files a second task without the dialog remembering the first", async ({ page }) => {
  // "File another" is the multi-capture path, and a form that came back still holding
  // the last task's prose would file the same thing twice.
  // The resolved project, not "/app/", which renders a redirect card first: the control
  // is mounted there too (that is the point of the global mount), and clicking it on
  // the way through opens a dialog the redirect then unmounts.
  await page.goto("/app/p/_local");
  await page.getByRole("button", { name: "New task or issue" }).click();
  const dialog = page.getByRole("dialog", { name: "New task" });

  await dialog.getByRole("textbox", { name: /^Title/ }).fill("The first one");
  await dialog.getByRole("textbox", { name: /^What happened/ }).fill("Filed first.");
  await dialog.getByRole("button", { name: "File it" }).click();
  await expect(dialog).toContainText("Filed as");

  // The start-an-agent box is there and off (task-176). Whether it is *enabled* is the
  // server's answer and depends on whether another spec has already switched dispatch
  // on for this project, so it is deliberately not asserted here -- which gate is shut
  // is covered where the state can be stated rather than inherited.
  await dialog.getByRole("button", { name: "File another" }).click();
  const startNow = dialog.getByRole("checkbox", { name: /Start an agent on it now/ });
  await expect(startNow).toBeVisible();
  await expect(startNow).not.toBeChecked();
  await expect(dialog.getByRole("textbox", { name: /^Title/ })).toHaveValue("");
  await expect(dialog.getByRole("textbox", { name: /^What happened/ })).toHaveValue("");
  await expect(dialog.getByRole("button", { name: "Add the full specification" })).toBeVisible();
});
