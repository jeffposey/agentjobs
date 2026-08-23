import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

/**
 * The queue-move check, end to end: a person promotes blocked work and is told.
 *
 * The jsdom suite already covers the notice's own behaviour against a stubbed handler.
 * What only a live server proves is the seam between them -- that the browser asks for
 * the envelope, that the route puts the check's sentences in it, that Keep reaches
 * `queue-keep`, and that the anchor ends up in the YAML where task-217 will read it.
 *
 * The API assertions at the end are the point. Everything on screen would look
 * identical if the notice were being composed in the browser.
 */

async function file(
  request: APIRequestContext,
  title: string,
  extra: Record<string, unknown> = {},
) {
  const response = await request.post("/api/tasks", {
    data: {
      title,
      description: "Seeded for the queue-move check.",
      summary: `Queue-check fixture: ${title}.`,
      priority: "critical",
      lifecycle: "ready",
      actor: "E2E Human",
      ...extra,
    },
  });
  expect(response.ok()).toBeTruthy();
  return (await response.json()).id as string;
}

async function order(page: Page, seeded: Array<string>) {
  const rendered = await page
    .locator("[data-task]")
    .evaluateAll((rows) => rows.map((row) => row.getAttribute("data-task") ?? ""));
  return rendered.filter((id) => seeded.includes(id));
}

function grip(page: Page, taskId: string) {
  return page.getByRole("button", { name: new RegExp(`^Reorder ${taskId},`) });
}

function notice(page: Page) {
  return page.getByTestId("queue-move-notice");
}

test("tells a person when they promote work the queue will skip, and records that they kept it", async ({
  page,
  request,
}) => {
  const gate = await file(request, "Check gate");
  const blocked = await file(request, "Check blocked", {
    dependencies: [{ task: gate, type: "needs" }],
  });
  const seeded = [gate, blocked];

  await page.goto("/app/p/_local/tasks");
  await expect.poll(() => order(page, seeded)).toEqual([gate, blocked]);

  await grip(page, blocked).focus();
  await page.keyboard.press("Alt+ArrowUp");

  // The move landed regardless -- the check reports, it never refuses.
  await expect.poll(() => order(page, seeded)).toEqual([blocked, gate]);

  // The sentence the server composed, not a container the browser rendered.
  await expect(notice(page)).toContainText("cannot be claimed where you have just put it");
  await expect(notice(page)).toContainText(gate);
  await expect(notice(page)).toHaveAttribute("role", "status");
  // Non-blocking: the rest of the list is still operable behind it.
  await expect(grip(page, gate)).toBeVisible();

  await page.getByRole("button", { name: "Keep it here" }).click();
  await expect(notice(page)).toBeHidden();

  // The anchor, in the record. This is the half task-217 reads.
  const record = await (await request.get(`/api/tasks/${blocked}`)).json();
  const move = record.log.filter((entry: { type: string }) => entry.type === "queue_move").at(-1);
  expect(move.data.warnings.map((item: { kind: string }) => item.kind)).toContain(
    "promoted_unclaimable",
  );
  const anchor = record.log.find(
    (entry: { data: Record<string, unknown> }) => entry.data.queue_anchor === "strong",
  );
  expect(anchor.type).toBe("decision");
  expect(anchor.re).toBe(move.id);
  expect(anchor.data.kept_over.length).toBe(move.data.warnings.length);
});

test("undo puts the order back, and the server agrees", async ({ page, request }) => {
  const gate = await file(request, "Undo gate");
  const blocked = await file(request, "Undo blocked", {
    dependencies: [{ task: gate, type: "needs" }],
  });
  const seeded = [gate, blocked];

  await page.goto("/app/p/_local/tasks");
  await expect.poll(() => order(page, seeded)).toEqual([gate, blocked]);

  await grip(page, blocked).focus();
  await page.keyboard.press("Alt+ArrowUp");
  await expect(notice(page)).toBeVisible();

  await page.getByRole("button", { name: "Undo the move" }).click();

  // The reload is the assertion: an optimistic list would show this either way.
  await page.reload();
  await expect.poll(() => order(page, seeded)).toEqual([gate, blocked]);

  const record = await (await request.get(`/api/tasks/${blocked}`)).json();
  const moves = record.log.filter((entry: { type: string }) => entry.type === "queue_move");
  expect(moves).toHaveLength(2);
  expect(moves.at(-1).actor).toBe("E2E Human");
});

test("an ordinary move says nothing at all", async ({ page, request }) => {
  const first = await file(request, "Quiet first");
  const second = await file(request, "Quiet second");
  const seeded = [first, second];

  await page.goto("/app/p/_local/tasks");
  await expect.poll(() => order(page, seeded)).toEqual([first, second]);

  await grip(page, second).focus();
  await page.keyboard.press("Alt+ArrowUp");
  await expect.poll(() => order(page, seeded)).toEqual([second, first]);

  await expect(notice(page)).toBeHidden();
});
