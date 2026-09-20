import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

/**
 * task-487: a front-end budget on the interaction task-135 made 36x faster.
 *
 * task-130's acceptance criterion ac-2 was "click to rendered under 500 ms". task-135
 * met it at 263 ms, down from 9,567 ms -- and then nothing guarded it. `scripts/bench.py`
 * measures the browser, but the benchmark is deliberately not part of `scripts/check.py`,
 * so from August until this spec the React app could regress without limit and the gate
 * stayed green.
 *
 * This is the seam: the `e2e` stage has already built the bundle and has a browser and a
 * live server up, so the marginal cost of timing one interaction is small. No new stage.
 *
 * **Click to rendered, not click to response.** The stop condition is the Full
 * specification region being visible -- the same signal `e2e-bench/open-task.bench.ts`
 * uses, so a figure here and a figure from `bench.py` are the same measurement. A fast
 * endpoint behind a component that paints nothing until every field arrives still feels
 * slow, and only the rendered timing notices.
 *
 * ## What this is not evidence of
 *
 * An automated gesture is not a person's gesture. ENGINEERING.md's verification section
 * and task-225 are explicit that Playwright's synthetic input proves handlers and routes
 * work and proves nothing about a gesture. Timing a render after a synthetic click is a
 * legitimate measurement of the render; **a green budget here is not a claim that
 * clicking a task row works well for a person.**
 *
 * ## What it does not warm
 *
 * task-207's lesson is that a test which sets up the state it is meant to be checking
 * measures nothing. So: a different row is clicked on every iteration, no detail is
 * fetched before the clock starts, and the first iteration -- the one that pays for
 * whatever the detail route costs cold -- is kept in the samples rather than discarded
 * as a warm-up.
 */

/**
 * The corpus these budgets run against, seeded by this spec.
 *
 * `frontend/e2e/run_server.py` serves a fresh temporary project, so without this the
 * list would hold whatever handful of rows the other specs happen to have open --
 * a corpus whose size is an accident of alphabetical file order.
 *
 * **Sixty is enough for the timing budget below and is not enough for a per-row one,
 * and that is a deliberate split rather than an oversight.** The timing budget is an
 * order-of-magnitude catastrophe check: it catches a collapse that is per-interaction,
 * which is the shape of every regression this interaction has actually had. A
 * *per-record* regression -- the class task-131 fixed, where one request walked the
 * whole corpus -- costs a few milliseconds per record, so at sixty records it would
 * hide entirely inside an order-of-magnitude threshold. The real corpus is several
 * hundred tasks; seeding several hundred here would cost the gate more than the
 * measurement is worth and would still be guessing at next year's size.
 *
 * That gap is what the payload budget is for. Bytes are a stable counter -- they mean
 * the same thing on every machine and at any corpus size -- so the per-record class is
 * caught there, by measuring bytes *per task*, and not here.
 */
const CORPUS_SIZE = 60;

/** Only this spec's rows, so `?q=` narrows the list to a corpus of stated size. */
const TOKEN = "gh487budget";

/**
 * Click-to-rendered, in milliseconds.
 *
 * **This is an order-of-magnitude check and must not be tightened into flakiness.**
 * The point is to catch 263 ms becoming 2.6 seconds, never 263 ms becoming 320 ms. It
 * is wall-clock time in a real browser on a machine that routinely runs three gates at
 * once (ENGINEERING.md, "Overlapping gates are the normal case"), so a threshold set
 * near the measured value would fail on contention, and a performance test that fails
 * on a busy machine gets disabled -- and a disabled test catches nothing at all. The
 * same reasoning, and the same warning, as `CATASTROPHE_SECONDS` in
 * `tests/test_performance_budgets.py`.
 *
 * Measured at 188ms median on this corpus, samples 175-200ms, on an unloaded machine
 * on 2026-09-19. The budget is thirteen times that, and it is set where it is because
 * task-135's figure was 263ms: a budget above 2.6 seconds would not notice that number
 * growing tenfold, which is the single thing this is here to see.
 *
 * Tighten it only deliberately, and only with a before/after pair from `bench.py` or
 * from this spec to say what the new number is derived from. The spec prints its
 * median on every run, pass or fail, so the headroom is always on the record.
 */
const CLICK_TO_RENDERED_MS = 2_500;

/**
 * Bytes of list payload per task, before the list can paint.
 *
 * A counter rather than a clock, and the difference decides how tightly it is set.
 * Bytes are exact: the same fixtures produce the same payload on every machine and
 * under any load, so nothing here can fail for being slightly unlucky and the
 * loose-or-be-disabled reasoning above does not apply. **This one is held close on
 * purpose.**
 *
 * It exists because the clock cannot see a *per-row* cost at sixty rows, and per-row
 * is the shape of the regressions this application has actually had. Both of them:
 * task-131's defect was one request walking the corpus 476 times, and task-484's was
 * `GET /tasks` answering with whole records -- spec prose, acceptance criteria and the
 * entire log of every task -- to draw a column of titles, which on the real backlog
 * was 10.4 MB. That route now answers with `TaskSummaryRead`, a listing row, and
 * whole records moved to `/tasks/full`.
 *
 * So what this guards is task-484's win, which otherwise has exactly as little behind
 * it as task-135's did. Measured at **601 bytes per task** on 2026-09-19 against these
 * fixtures; it was 3,435 before the projection landed. The ceiling is 1,500 -- room
 * for a field or two to be added to a listing row deliberately, and low enough that
 * the projection quietly coming undone fires it.
 *
 * A legitimate growth past this is a one-line change with a re-measured number beside
 * it. That is the point: the alternative is a ceiling so high it notices nothing.
 */
const LIST_BYTES_PER_TASK = 1_500;

/** Timed iterations. Five, because the assertion is on the median of them. */
const ITERATIONS = 5;

/**
 * How long a single iteration is allowed to wait for the detail before Playwright gives
 * up on the locator.
 *
 * Far above the budget on purpose. Playwright's default is five seconds, and at that
 * value a regression worse than five seconds per click stops being *measured* and
 * becomes "element not found" -- so the failure would name a locator instead of a
 * number, on exactly the collapse this spec exists to report. task-135's starting point
 * was 9,567ms; twenty seconds keeps a regression of that size inside the measurement,
 * where the budget can print it.
 *
 * It costs nothing on a healthy run, which is every run where the budget passes.
 */
const RENDER_TIMEOUT_MS = 20_000;

/**
 * The list, narrowed to this spec's own corpus.
 *
 * `?q=` and the list's default `open` status together, rather than the `status=all` the
 * bench spec uses: `afterAll` closes these fixtures but nothing deletes them, so on a
 * second pass through this file -- which is what Playwright does with a fresh worker
 * after a failure -- `status=all` would show the previous pass's closed rows as well.
 * The corpus this measures has to be the size this file states, or the number means
 * nothing and the count assertion below fires instead of the budget.
 */
const LIST_URL = `/app/p/_local/tasks?q=${TOKEN}`;

/** The list endpoint the page fetches before it can paint anything. */
const LIST_ENDPOINT = "/api/projects/_local/tasks";

/**
 * A description of realistic weight.
 *
 * A fixture with a one-line spec would make the payload budget vacuous: a list that
 * went back to shipping every record's prose would ship almost nothing, because these
 * records would have almost nothing to ship, and the budget would sit green through
 * exactly the regression task-484 fixed. Real tasks in this repository carry several
 * kilobytes of specification, so these do too.
 */
const DESCRIPTION = (
  "A seeded fixture with a specification of realistic weight, so that a payload " +
  "measured against it means something. A listing row does not carry this text; a " +
  "whole record does, which is the difference the budget is watching for. "
).repeat(12);

/** The ids this spec put in the list, so `afterAll` can take them back out. */
const seeded: string[] = [];

async function seedCorpus(request: APIRequestContext): Promise<void> {
  for (let index = 0; index < CORPUS_SIZE; index += 1) {
    const response = await request.post("/api/tasks", {
      data: {
        // Numbered, so the rows are distinguishable in a trace and the click target
        // of a failing iteration can be identified from the report alone.
        title: `${TOKEN} fixture ${String(index).padStart(3, "0")}`,
        summary: "Exists so the front-end budget has a corpus of stated size.",
        description: DESCRIPTION,
        lifecycle: "ready",
        category: "testing",
        actor: "E2E Human",
      },
    });
    expect(response.ok(), `seeding fixture ${index} failed`).toBeTruthy();
    seeded.push((await response.json()).id as string);
  }
}

/**
 * The rows on screen, by the same locator the bench spec uses.
 *
 * Kept identical on purpose: a number from this gate and a number from
 * `scripts/bench.py` should be comparable, and they are not if the two stop the clock
 * on different elements.
 */
async function taskLinks(page: Page) {
  const region = page.getByRole("region", { name: "Tasks" });
  await expect(region).toBeVisible();
  const links = region.getByRole("link");
  await expect(links.first()).toBeVisible();
  return links;
}

function median(samples: number[]): number {
  const sorted = [...samples].sort((left, right) => left - right);
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2 === 0 ? (sorted[middle - 1] + sorted[middle]) / 2 : sorted[middle];
}

test.describe("the front-end interaction has a budget", () => {
  // Built here rather than taken from the `request` fixture, which Playwright disposes
  // when `beforeAll` returns -- the `afterAll` that puts the corpus back would then
  // fail on a closed context and leave sixty open rows in every later spec's list.
  let api: APIRequestContext;

  test.beforeAll(async ({ playwright, baseURL }) => {
    api = await playwright.request.newContext({ baseURL });
    const started = Date.now();
    await seedCorpus(api);
    // What this spec costs the `e2e` stage is the seed plus the two tests, and the seed
    // is the larger half. Printed rather than left to be re-derived: the next person to
    // ask whether the corpus can afford to be bigger should not have to measure it.
    console.log(`[budget] seeded ${CORPUS_SIZE} fixtures in ${Date.now() - started}ms`);
  });

  test.afterAll(async () => {
    const started = Date.now();
    for (const id of seeded) {
      const closed = await api.post(`/api/tasks/${id}/close`, {
        data: { actor: "E2E Human", outcome: "cancelled", body: "Budget fixture." },
      });
      expect(closed.ok()).toBeTruthy();
    }
    console.log(`[budget] closed ${seeded.length} fixtures in ${Date.now() - started}ms`);
    await api.dispose();
  });

  test("clicking a task row renders its detail within an order of magnitude", async ({ page }) => {
    // Five iterations that may each wait RENDER_TIMEOUT_MS, plus the page loads around
    // them, do not fit the default thirty seconds when the application is broken. A
    // healthy run takes about two seconds and never approaches this.
    test.setTimeout(ITERATIONS * RENDER_TIMEOUT_MS + 60_000);

    await page.goto(LIST_URL);
    const links = await taskLinks(page);
    const available = await links.count();
    // The corpus is this spec's own, so the count is a fact and not a floor: a
    // different number means the fixtures did not all land, and every figure below
    // would then be measured against a corpus nobody stated.
    expect(available, "the seeded corpus did not reach the list").toBe(CORPUS_SIZE);

    const samples: number[] = [];
    for (let index = 0; index < ITERATIONS; index += 1) {
      const rows = await taskLinks(page);
      // A different row every iteration. Clicking one row repeatedly would measure a
      // cache hit -- the state under test, set up by the test (task-207).
      const target = rows.nth(index % available);
      await target.scrollIntoViewIfNeeded();

      const started = Date.now();
      await target.click();
      await expect(page.getByRole("region", { name: "Full specification" })).toBeVisible({
        timeout: RENDER_TIMEOUT_MS,
      });
      samples.push(Date.now() - started);

      await page.goBack();
    }

    // The median, not the worst sample: one hiccup on a machine running three gates is
    // not the collapse this is looking for, and a budget that fires on it would be
    // turned off within a week. Every sample is in the message, so a failure says what
    // it measured rather than only that something was slow.
    const measured = median(samples);
    // Printed on a pass as well as a failure. A budget that speaks only when it breaks
    // tells nobody how much room is left, and the figure is what a later reader needs
    // in order to tighten the constant deliberately rather than by guess.
    console.log(
      `[budget] click task row -> detail rendered (warm app), ${CORPUS_SIZE} tasks: ` +
        `median ${measured}ms [${samples.join(", ")}] of ${CLICK_TO_RENDERED_MS}ms`,
    );
    expect(
      measured,
      `click task row -> detail rendered (warm app), ${CORPUS_SIZE} tasks: median ` +
        `${measured}ms over ${ITERATIONS} iterations [${samples.join(", ")}] against an ` +
        `order-of-magnitude budget of ${CLICK_TO_RENDERED_MS}ms. This is click to ` +
        "*rendered* -- the clock stops when the Full specification region is visible.",
    ).toBeLessThanOrEqual(CLICK_TO_RENDERED_MS);
  });

  test("the list fetches a bounded number of bytes per task before it paints", async ({ page }) => {
    // Captured from the wire rather than computed from the fixtures: the claim is about
    // what the browser was handed, and a regression that starts shipping more of each
    // record shows up here and nowhere in the DOM.
    let payload: { bytes: number; tasks: number } | null = null;
    page.on("response", async (response) => {
      if (payload !== null) return;
      const url = new URL(response.url());
      if (url.pathname !== LIST_ENDPOINT) return;
      const body = await response.body().catch(() => null);
      if (body === null) return;
      const parsed = JSON.parse(body.toString("utf-8")) as unknown[];
      payload = { bytes: body.byteLength, tasks: parsed.length };
    });

    await page.goto(LIST_URL);
    await taskLinks(page);
    await expect.poll(() => payload).not.toBeNull();

    const { bytes, tasks } = payload!;
    expect(tasks, "the list response carried no tasks to divide by").toBeGreaterThan(0);
    const perTask = Math.round(bytes / tasks);
    console.log(
      `[budget] list payload before first paint: ${bytes} bytes for ${tasks} tasks ` +
        `(${perTask} each) of ${LIST_BYTES_PER_TASK} each`,
    );
    expect(
      perTask,
      `the list view fetched ${bytes} bytes for ${tasks} tasks (${perTask} bytes each) ` +
        `before it could paint, against a budget of ${LIST_BYTES_PER_TASK} bytes per ` +
        "task. A listing row is not a record: if the list is answering with whole " +
        "tasks again, that is task-484 coming undone. If a field was added to the row " +
        "deliberately, re-measure and move the constant in the same commit.",
    ).toBeLessThanOrEqual(LIST_BYTES_PER_TASK);
  });
});
