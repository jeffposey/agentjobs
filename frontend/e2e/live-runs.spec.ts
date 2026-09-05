import { expect, test } from "@playwright/test";

/**
 * The machine-wide live-run surfaces, against a real run (task-328).
 *
 * The interesting assertions are the ones only a live server can make: that a run
 * started from a task page appears on the Runs tab and in the nav badge **without a
 * reload**, and that both go back to zero when it ends. A jsdom test renders a fixture,
 * which proves the markup and proves nothing about the freshness that is the whole
 * point of a "running now" screen -- ENGINEERING.md, Verification.
 *
 * It shares one server and one project with every other spec in this directory, so it
 * enables dispatch, uses it, and puts both back.
 */

const project = "/app/p/_local";

/** Below `NAV_INLINE_MIN_PX` the destinations are behind the burger. */
const NAV_INLINE_MIN_PX = 1140;

async function openRunsTab(page: import("@playwright/test").Page) {
  if ((page.viewportSize()?.width ?? 0) < NAV_INLINE_MIN_PX) {
    await page.getByRole("button", { name: "Navigation" }).click();
  }
  await page.getByRole("link", { name: /^Runs/ }).click();
  await expect(page).toHaveURL(new RegExp(`${project}/runs$`));
}

const badge = (page: import("@playwright/test").Page) =>
  page.getByTestId("live-run-count").first();

test("an idle machine says so, in the badge and on both surfaces", async ({ page }) => {
  await page.goto(project);

  // Zero, rendered. Not absent: a badge that disappears when idle cannot be told from
  // one that has stopped polling, which is the failure mode this number exists to rule
  // out.
  await expect(badge(page)).toHaveText("0");
  await expect(page.getByTestId("machine-capacity")).toContainText("nothing running");

  await openRunsTab(page);
  await expect(page.getByTestId("no-live-runs")).toBeVisible();
});

test("a real run appears on both surfaces without a reload, and leaves when it ends", async ({
  page,
}) => {
  await page.goto(`${project}/dispatch`);
  await page.getByRole("button", { name: /enable dispatch/i }).click();
  await expect(page.getByRole("button", { name: /disable dispatch/i })).toBeVisible();

  await page.getByRole("link", { name: "Create", exact: true }).click();
  await page.getByRole("textbox", { name: "Title", exact: true }).fill("Watch me run");
  await page.getByRole("textbox", { name: /^Summary/ }).fill("Seen from the Runs tab.");
  await page
    .getByRole("textbox", { name: /^Working description/ })
    .fill("Proves the machine-wide surfaces notice a run they did not start.");
  await page.getByRole("radio", { name: /^Ready/ }).check();
  await page.getByRole("button", { name: "Create task" }).click();

  await page.getByRole("region", { name: "Tasks" }).getByText("Watch me run").click();
  const taskUrl = page.url();
  const dispatch = page.getByRole("region", { name: "Dispatch" });
  await dispatch.getByRole("button", { name: /dispatch/i }).click();
  await expect(dispatch.getByRole("listitem").first()).toContainText("Running", {
    timeout: 15_000,
  });

  // The badge is in the header of the page already open. Nothing here reloads it: if
  // this passes, the machine-wide query is polling on its own clock.
  await expect(badge(page)).toHaveText("1", { timeout: 15_000 });

  await openRunsTab(page);
  const row = page.getByRole("row").filter({ hasText: "Watch me run" });
  await expect(row).toContainText("Working");
  await expect(row).toContainText("End-to-end project");
  await expect(row.getByRole("link").first()).toHaveAttribute(
    "href",
    new RegExp(`/p/_local/tasks/${taskUrl.split("/").pop()}$`),
  );
  await expect(page.getByTestId("capacity-sentence")).toContainText("1 of");

  // Read-only: the surface shows the run and offers no way to act on it (task-312).
  await expect(page.getByRole("button", { name: /cancel/i })).toHaveCount(0);

  // End the run from the task page, then come back without reloading the Runs tab.
  await page.goto(taskUrl);
  await page
    .getByRole("region", { name: "Dispatch" })
    .getByRole("button", { name: /cancel run/i })
    .click();
  await expect(page.getByRole("region", { name: "Dispatch" })).toContainText("Cancelled", {
    timeout: 15_000,
  });

  await openRunsTab(page);
  await expect(page.getByTestId("no-live-runs")).toBeVisible({ timeout: 15_000 });
  await expect(badge(page)).toHaveText("0");

  // Put the shared project back: cancelling hands the ball to a human, and this task
  // would otherwise be every later spec's dashboard headline.
  await page.goto(taskUrl);
  await page
    .getByRole("region", { name: "Review actions" })
    .getByRole("button", { name: /reject/i })
    .click();
  await page.getByLabel("Reason for rejection").fill("End-to-end run finished with it.");
  await page.getByRole("button", { name: "Submit" }).click();

  await page.goto(`${project}/dispatch`);
  await page.getByRole("button", { name: /disable dispatch/i }).click();
  await expect(page.getByRole("button", { name: /enable dispatch/i })).toBeVisible();
});
