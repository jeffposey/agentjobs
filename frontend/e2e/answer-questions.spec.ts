import { expect, test, type APIRequestContext } from "@playwright/test";

/**
 * Answering an agent's questions on a phone (task-017).
 *
 * The viewport is the point, not decoration. This feature exists because Jeff reads
 * these handoffs on a phone, where task-077's four questions meant four paragraphs of
 * thumb-typing; the whole claim being made is that the same four are now four taps. So
 * the test is run at 390x844 -- an iPhone 14 -- and the answers are given by clicking,
 * with the keyboard used exactly once, for the question that wants a number nobody
 * offered.
 *
 * **No gesture opens the form.** It was behind an "Answer Questions" button for one
 * release; Jeff: *"It should not require pressing answer questions button to get the
 * multiple choice question prompt, that should be in default view."* So the first thing
 * every test below does after `goto` is assert on an option, with nothing in between --
 * a click there would hide the regression it is meant to catch.
 *
 * It runs against the real server, so one pass covers the typed payload, the handoff
 * route, the generated client and the rendered page together. The answers are then read
 * back out of the API rather than off the screen: what matters is what the record holds.
 */

const PHONE = { width: 390, height: 844 };

//: task-077's real handoff, 2026-08-18, trimmed to three questions. Q3 is the one Jeff
//: answered with a number none of the options offered.
const QUESTIONS = [
  {
    body: "Session mode, or supervise our own processes?",
    options: [
      {
        label: "Session mode primary, batch retained",
        description: "Keeps a mid-flight redirect; batch stays a declared runner mode.",
        recommended: true,
      },
      { label: "Session-only, delete batch", description: "One code path, no spend ceiling." },
      { label: "Batch-only", description: "Spend ceiling, but no mid-flight redirect." },
    ],
  },
  {
    body: "How long before an idle session counts as stalled?",
    placeholder: "a number of minutes",
    options: [
      { label: "15 minutes", description: "More false positives if an agent pauses." },
      { label: "4 hours", description: "Only catches overnight stalls." },
    ],
  },
  {
    body: "What happens to task-070 and task-072?",
    multi_select: true,
    options: [
      { label: "Rescope task-070 to batch mode", recommended: true },
      { label: "Rescope task-072 to our own ledger", recommended: true },
      { label: "Close task-072 as superseded" },
    ],
  },
];

type LogEntry = { id: number; type: string; re?: number | null; body?: string | null; data?: Record<string, unknown> };

/**
 * Every task this file creates, closed and archived when its test ends.
 *
 * Every spec in this directory shares one server and one project, so a task left open
 * here joins the human inbox and the backlog that later specs assert over -- and it
 * broke two of them the first time this file ran, by putting three rows between the two
 * that queue-order's cross-band drag has to span in one screen. Cleaning up is cheaper
 * than making every other spec immune to whatever happens to run before it.
 */
const created: Array<string> = [];

test.afterEach(async ({ request }) => {
  for (const taskId of created.splice(0)) {
    await request.post(`/api/tasks/${taskId}/close`, {
      data: { actor: "claude", outcome: "cancelled", body: "Fixture for the e2e run.", archive: true },
    });
  }
});

async function askThreeQuestions(request: APIRequestContext): Promise<string> {
  const response = await request.post("/api/tasks", {
    data: {
      title: "Dispatch session launcher",
      description: "sc-1 is verified; three decisions are needed before code depends on it.",
      category: "infra",
      lifecycle: "ready",
    },
  });
  expect(response.ok()).toBeTruthy();
  const taskId = String((await response.json()).id);

  const claimed = await request.post(`/api/tasks/${taskId}/claim`, { data: { agent: "claude" } });
  expect(claimed.ok()).toBeTruthy();

  const handed = await request.post(`/api/tasks/${taskId}/handoff`, {
    data: {
      actor: "claude",
      ball: "human",
      ball_reason: "decision",
      ball_prompt: "sc-1 verified against CLI 2.1.228. Three decisions before I can carry on.",
      questions: QUESTIONS,
    },
  });
  expect(handed.ok()).toBeTruthy();
  created.push(taskId);
  return taskId;
}

async function logOf(request: APIRequestContext, taskId: string): Promise<Array<LogEntry>> {
  const response = await request.get(`/api/tasks/${taskId}`);
  expect(response.ok()).toBeTruthy();
  return (await response.json()).log as Array<LogEntry>;
}

test("answers three questions from a phone, with one tap each and one number typed", async ({
  page,
  request,
}) => {
  const taskId = await askThreeQuestions(request);
  await page.setViewportSize(PHONE);
  await page.goto(`/app/p/_local/tasks/${taskId}`);

  const panel = page.getByRole("region", { name: "Review actions" });
  await expect(panel).toBeVisible();
  // task-231's rule still holds underneath this one: there is no branch here and
  // nothing to merge, so nothing offers to merge.
  await expect(panel.getByRole("button", { name: /Approve/ })).toHaveCount(0);
  // Nothing opens it: the questions are there on arrival, and the button that used to
  // reveal them is gone because it would open what is already open.
  await expect(panel.getByRole("button", { name: "✎ Answer Questions" })).toHaveCount(0);

  // Every question is on the page, in order, with the recommendation marked and
  // nothing chosen for the reader.
  for (const [index, question] of QUESTIONS.entries()) {
    await expect(panel.getByText(`${index + 1}. ${question.body}`)).toBeVisible();
  }
  await expect(panel.getByText("Recommended")).toHaveCount(3);
  await expect(panel.locator("button[aria-pressed='true']")).toHaveCount(0);

  // Nothing has been said yet, so there is nothing to submit.
  const submit = panel.getByRole("button", { name: "✓ Send answers" });
  await expect(submit).toBeDisabled();

  // Three taps and one number. No other typing anywhere on this page.
  await panel.getByRole("button", { name: /Session mode primary/ }).click();
  await panel.getByPlaceholder("a number of minutes").fill("60 minutes");
  await panel.getByRole("button", { name: /Rescope task-070/ }).click();
  await panel.getByRole("button", { name: /Rescope task-072/ }).click();

  // Every control a thumb has to hit is at least 44px tall, which is what the
  // `touch-target` class is for; a phone-width run is where that is worth asserting.
  for (const label of ["Session mode primary", "Rescope task-070", "Send answers"]) {
    const box = await panel.getByRole("button", { name: new RegExp(label) }).boundingBox();
    expect(box?.height ?? 0).toBeGreaterThanOrEqual(44);
  }
  // And the page does not scroll sideways at 390px, which is how a form of this size
  // usually goes wrong on a phone.
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(overflow).toBeLessThanOrEqual(0);

  await expect(submit).toBeEnabled();
  await submit.click();

  // The panel goes when the ball leaves the human, which is the state a person reads.
  await expect(panel).toBeHidden();

  const log = await logOf(request, taskId);
  const asked = log.filter((entry) => entry.type === "question");
  const given = log.filter((entry) => entry.type === "answer");

  expect(asked).toHaveLength(3);
  // Threaded by `re` to the question each one answers -- the record's half of sc-3.
  expect(given.map((entry) => entry.re)).toEqual(asked.map((entry) => entry.id));
  expect(given[0]?.data?.selected).toEqual(["Session mode primary, batch retained"]);
  expect(given[1]?.data?.selected).toEqual([]);
  expect(given[1]?.data?.other).toBe("60 minutes");
  expect(given[2]?.data?.selected).toEqual([
    "Rescope task-070 to batch mode",
    "Rescope task-072 to our own ledger",
  ]);

  // One handoff for the lot, carrying an ask the resuming agent can act on even
  // though nobody wrote it a sentence.
  const task = await (await request.get(`/api/tasks/${taskId}`)).json();
  expect(task.ball).toBe("agent");
  expect(task.ball_reason).toBe("answer");
  expect(task.ball_prompt).toContain("60 minutes");
  expect(task.ball_prompt).toContain("Session mode primary");
});

test("keeps the prose box on every question, and takes an answer that rejects every option", async ({
  page,
  request,
}) => {
  const taskId = await askThreeQuestions(request);
  await page.setViewportSize(PHONE);
  await page.goto(`/app/p/_local/tasks/${taskId}`);

  const panel = page.getByRole("region", { name: "Review actions" });
  // One per question, unconditionally -- not only where the agent supplied no options.
  await expect(panel.getByLabel("Something else")).toHaveCount(3);

  await panel
    .getByLabel("Something else")
    .first()
    .fill("None of these. Run it under a supervisor we already have.");
  await panel.getByRole("button", { name: "✓ Send answers" }).click();
  await expect(panel).toBeHidden();

  const given = (await logOf(request, taskId)).filter((entry) => entry.type === "answer");
  expect(given).toHaveLength(1);
  expect(given[0]?.data?.selected).toEqual([]);
  expect(given[0]?.body).toBe("None of these. Run it under a supervisor we already have.");
});

test("leaves an unanswered question open and answerable next time", async ({ page, request }) => {
  const taskId = await askThreeQuestions(request);
  await page.setViewportSize(PHONE);
  await page.goto(`/app/p/_local/tasks/${taskId}`);

  const panel = page.getByRole("region", { name: "Review actions" });
  await panel.getByRole("button", { name: /Batch-only/ }).click();
  await panel.getByRole("button", { name: "✓ Send answers" }).click();
  await expect(panel).toBeHidden();

  // Hand it back and the two questions nobody answered are still there, still open.
  // Decision 3 on task-017: a question outliving its handoff is a backlog, not a leak.
  const handed = await request.post(`/api/tasks/${taskId}/handoff`, {
    data: {
      actor: "claude",
      ball: "human",
      ball_reason: "decision",
      ball_prompt: "Still need the other two.",
    },
  });
  expect(handed.ok()).toBeTruthy();

  await page.reload();
  await expect(panel.getByText("2 open questions", { exact: false })).toBeVisible();
  await expect(panel.getByPlaceholder("a number of minutes")).toBeVisible();
  await expect(panel.getByText(/Session mode, or supervise/)).toHaveCount(0);
});
