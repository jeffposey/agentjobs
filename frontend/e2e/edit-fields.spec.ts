import { expect, test, type APIRequestContext } from "@playwright/test";

/**
 * Grooming a task from a phone (task-230).
 *
 * The viewport is the point rather than decoration. The reason this feature exists is
 * that raising a priority or fixing a tag meant an agent session or a hand-edited
 * record, so the fifteen minutes somebody has on a phone produced nothing; a form that
 * only works at desktop width has not delivered it. So every test here runs at 390x844.
 *
 * It runs against the real server, so one pass covers the rendered control, the typed
 * payload, the generated client and the PATCH route together — and the two failures
 * worth having are ones jsdom cannot produce at all: a stale revision refused by the
 * server, and an edit attributed to the wrong actor. Both are read back out of the API
 * rather than off the screen, because what matters is what the record holds.
 */

const PHONE = { width: 390, height: 844 };

type TaskRecord = {
  id: string;
  title: string;
  priority: string;
  category: string;
  effort: string | null;
  tags: Array<string>;
  ball: string | null;
  ball_reason: string | null;
  ball_prompt: string | null;
  log: Array<{ actor: string; type: string; body: string; data?: Record<string, unknown> }>;
};

/** Every task this file creates, closed and archived when its test ends. */
const created: Array<string> = [];

test.afterEach(async ({ request }) => {
  for (const taskId of created.splice(0)) {
    await request.post(`/api/tasks/${taskId}/close`, {
      data: { actor: "claude", outcome: "cancelled", body: "Fixture for the e2e run.", archive: true },
    });
  }
});

async function makeTask(
  request: APIRequestContext,
  overrides: Record<string, unknown> = {},
): Promise<string> {
  const response = await request.post("/api/tasks", {
    data: {
      title: "Groom me",
      summary: "A task filed so its fields can be edited.",
      description: "The working spec is not what this task is about.",
      category: "feature",
      priority: "medium",
      effort: "half a day",
      tags: ["gui", "grooming"],
      lifecycle: "ready",
      ...overrides,
    },
  });
  expect(response.ok()).toBeTruthy();
  const taskId = String((await response.json()).id);
  created.push(taskId);
  return taskId;
}

async function recordOf(request: APIRequestContext, taskId: string): Promise<TaskRecord> {
  const response = await request.get(`/api/tasks/${taskId}`);
  expect(response.ok()).toBeTruthy();
  return (await response.json()) as TaskRecord;
}

test("edits every tier-one field from a phone, and the record shows who did it", async ({
  page,
  request,
}) => {
  const taskId = await makeTask(request);
  await page.setViewportSize(PHONE);
  await page.goto(`/app/p/_local/tasks/${taskId}`);

  await page.getByRole("button", { name: "Edit fields" }).click();
  const panel = page.getByRole("region", { name: "Task fields" });
  await expect(panel).toBeVisible();

  await panel.getByLabel("Title").fill("Groomed from a phone");
  await panel.getByLabel("Priority").selectOption("critical");
  await panel.getByLabel("Category").fill("chore");
  await panel.getByLabel("Effort").fill("twenty minutes");
  await panel.getByRole("button", { name: "Remove tag grooming" }).click();
  await panel.getByLabel("Add a tag").fill("phone");
  await panel.getByRole("button", { name: "Add" }).click();
  await panel.getByRole("button", { name: "Save fields" }).click();

  // Rendered values, not markup: the header is where a reader sees these, and it is
  // what has to be right.
  await expect(page.getByRole("heading", { name: "Groomed from a phone" })).toBeVisible();
  // The record's own header, not the app's pinned nav above it.
  const heading = page.locator("header[data-pinned]");
  await expect(heading).toContainText("critical");
  await expect(heading).toContainText("chore");
  await expect(heading).toContainText("phone");
  await expect(heading).not.toContainText("grooming");
  await expect(page.getByRole("region", { name: "Task metadata" })).toContainText("twenty minutes");

  const record = await recordOf(request, taskId);
  expect(record.title).toBe("Groomed from a phone");
  expect(record.priority).toBe("critical");
  expect(record.category).toBe("chore");
  expect(record.effort).toBe("twenty minutes");
  expect(record.tags).toEqual(["gui", "phone"]);

  // Attributed to the person at the browser, and naming what it touched. An edit whose
  // author the log cannot resolve is the failure the append-only log exists to prevent.
  const edit = record.log.find((entry) => entry.body.startsWith("Updated "));
  expect(edit).toBeDefined();
  expect(edit?.actor).toBe("E2E Human");
  expect(edit?.body).toContain("category");
  expect(edit?.body).toContain("priority");
  expect(edit?.body).toContain("tags");
  expect(edit?.body).toContain("title");

  // Visible in the log the page renders, too.
  await expect(page.getByRole("region", { name: "Task log" })).toContainText("E2E Human");
});

test("an edit against a stale read is refused, and the conflict names the field that moved", async ({
  page,
  request,
}) => {
  const taskId = await makeTask(request);
  await page.setViewportSize(PHONE);
  await page.goto(`/app/p/_local/tasks/${taskId}`);

  await page.getByRole("button", { name: "Edit fields" }).click();
  const panel = page.getByRole("region", { name: "Task fields" });
  await panel.getByLabel("Effort").fill("twenty minutes");

  // Somebody else moves the task while this page still holds the revision it loaded.
  const elsewhere = await request.patch(`/api/tasks/${taskId}`, { data: { priority: "low" } });
  expect(elsewhere.ok()).toBeTruthy();

  await panel.getByRole("button", { name: "Save fields" }).click();

  // Refused, explained in terms of the field rather than of two timestamps, and the
  // typing kept so it can be saved again.
  const alert = panel.getByRole("alert");
  await expect(alert).toContainText("changed while you had it open");
  await expect(alert).toContainText("priority went from medium to low");
  await expect(panel.getByLabel("Effort")).toHaveValue("twenty minutes");

  // Nothing was written by the refused save.
  expect((await recordOf(request, taskId)).effort).toBe("half a day");

  // Saving again, now against the record that was re-read, applies the edit — and does
  // not put the priority back to what the stale page was holding.
  await panel.getByRole("button", { name: "Save fields" }).click();

  // Waited on the edited value appearing where a reader would look for it, which the
  // page can only render once the PATCH has been written and re-read.
  //
  // The obvious signal — the conflict banner going away — is not one. The page clears
  // the error at the top of the save, before the request is sent, so `not.toContainText`
  // is satisfied on the click; reading the API immediately after it raced the write and
  // turned the gate red on unrelated branches (task-397).
  await expect(page.getByRole("region", { name: "Task metadata" })).toContainText(
    "twenty minutes",
  );
  // And the form is gone, which happens only on the save's resolution: a refused save
  // keeps it open with the typing in it, so this distinguishes applied from refused.
  await expect(panel).toBeHidden();

  const record = await recordOf(request, taskId);
  expect(record.effort).toBe("twenty minutes");
  expect(record.priority).toBe("low");
});

test("editing a task parked at review leaves it parked at review", async ({ page, request }) => {
  const taskId = await makeTask(request);
  const claimed = await request.post(`/api/tasks/${taskId}/claim`, { data: { agent: "claude" } });
  expect(claimed.ok()).toBeTruthy();
  const prompt = "Branch is green and rebased. Read the diff and approve or send it back.";
  const handed = await request.post(`/api/tasks/${taskId}/handoff`, {
    data: { actor: "claude", ball: "human", ball_reason: "review", ball_prompt: prompt },
  });
  expect(handed.ok()).toBeTruthy();

  await page.setViewportSize(PHONE);
  await page.goto(`/app/p/_local/tasks/${taskId}`);
  await expect(page.getByRole("region", { name: "Review actions" })).toBeVisible();

  await page.getByRole("button", { name: "Edit fields" }).click();
  const panel = page.getByRole("region", { name: "Task fields" });
  await panel.getByLabel("Priority").selectOption("high");
  await panel.getByRole("button", { name: "Save fields" }).click();

  // The priority moved and nothing else did. The review panel is still on screen with
  // the same ask, because an edit is not a workflow move.
  await expect(page.locator("header[data-pinned]")).toContainText("high");
  await expect(page.getByRole("region", { name: "Review actions" })).toBeVisible();
  await expect(page.getByRole("region", { name: "Review actions" })).toContainText(prompt);

  const record = await recordOf(request, taskId);
  expect(record.priority).toBe("high");
  expect(record.ball).toBe("human");
  expect(record.ball_reason).toBe("review");
  expect(record.ball_prompt).toBe(prompt);
});

test("the whole form fits the phone it is meant for", async ({ page, request }) => {
  const taskId = await makeTask(request);
  await page.setViewportSize(PHONE);
  await page.goto(`/app/p/_local/tasks/${taskId}`);

  // The two openers, closed. They are icon-only since task-230's review, which makes
  // them the controls most at risk of being too small to hit: a glyph in a box shrinks
  // to the glyph unless something holds the box open. They also have to share one line
  // rather than stacking, which is the whole point of making them icons.
  const openers = ["Edit fields", "Add a note"];
  const boxes = [];
  for (const name of openers) {
    const box = await page.getByRole("button", { name }).boundingBox();
    expect(box, `${name} has no box`).not.toBeNull();
    expect(box!.height, `${name} is ${box!.height}px tall`).toBeGreaterThanOrEqual(44);
    expect(box!.width, `${name} is ${box!.width}px wide`).toBeGreaterThanOrEqual(44);
    boxes.push(box!);
  }
  expect(boxes[0]!.y, "the two openers are not on one line").toBeCloseTo(boxes[1]!.y, 0);

  await page.getByRole("button", { name: "Edit fields" }).click();
  const panel = page.getByRole("region", { name: "Task fields" });

  // Nothing runs past the right-hand edge, and the document does not scroll sideways to
  // reach a control. A form that only lays out at desktop width is the failure mode this
  // asserts against.
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(overflow).toBeLessThanOrEqual(0);

  // Every control is at least a 44px tap target, which is the rule `.touch-target`
  // exists to keep and the reason this is checked in a browser rather than in jsdom.
  for (const name of ["Title", "Priority", "Category", "Effort", "Add a tag"]) {
    const box = await panel.getByLabel(name).boundingBox();
    expect(box, `${name} has no box`).not.toBeNull();
    expect(box!.height, `${name} is ${box!.height}px tall`).toBeGreaterThanOrEqual(44);
    expect(box!.width + box!.x, `${name} runs past the viewport`).toBeLessThanOrEqual(PHONE.width);
  }
  for (const name of ["Save fields", "Add", "Remove tag gui"]) {
    const box = await panel.getByRole("button", { name }).boundingBox();
    expect(box, `${name} has no box`).not.toBeNull();
    expect(box!.height, `${name} is ${box!.height}px tall`).toBeGreaterThanOrEqual(44);
  }
});

test("offers the words this project already uses, and only once asked", async ({ page, request }) => {
  await makeTask(request, { title: "Vocabulary donor", tags: ["donated-tag"], category: "donated" });
  const taskId = await makeTask(request, { title: "Vocabulary consumer" });

  await page.setViewportSize(PHONE);
  const listed: Array<string> = [];
  page.on("request", (event) => listed.push(event.url()));
  await page.goto(`/app/p/_local/tasks/${taskId}`);
  await expect(page.getByRole("button", { name: "Edit fields" })).toBeVisible();

  // The task list is the heaviest read this API offers, and a reader who opened this
  // page to read it must not pay for it.
  const isListing = (url: string) => /\/tasks(\?|$)/.test(new URL(url).pathname + new URL(url).search);
  expect(listed.filter((url) => url.includes("/api/") && isListing(url))).toEqual([]);

  await page.getByRole("button", { name: "Edit fields" }).click();
  const panel = page.getByRole("region", { name: "Task fields" });

  await expect
    .poll(() => panel.locator("#task-field-tags option[value='donated-tag']").count())
    .toBeGreaterThan(0);
  await expect
    .poll(() => panel.locator("#task-field-categories option[value='donated']").count())
    .toBeGreaterThan(0);
});
