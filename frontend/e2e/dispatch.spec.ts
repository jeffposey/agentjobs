import { expect, test } from "@playwright/test";

/**
 * The whole loop the dispatch work exists to close, through the real stack: turn
 * dispatch on for this project, start an agent from a click, watch the run, stop it,
 * and turn dispatch off again.
 *
 * This is the only place any of that is exercised against a live server. Every
 * assertion below is on text a person reads or a value their click acts on -- a check
 * that a `data-` attribute exists would pass just as well while the page rendered the
 * enum's own spelling, which is the failure this repository has shipped before.
 */

const project = "/app/p/_local";

test("turns dispatch on, starts a real agent process, cancels it, and turns it off", async ({
  page,
}) => {
  await page.goto(`${project}/dispatch`);

  // The machine is configured and the master switch is on; this project is not
  // trusted with it yet. That is the state a real machine is in after installing.
  const settings = page.getByRole("region", { name: "Dispatch settings" });
  await expect(settings.getByText("Machine-wide switch").locator("..")).toContainText("Open");
  await expect(settings.getByText("This project", { exact: true }).locator("..")).toContainText(
    "Closed",
  );

  await expect(page.getByLabel("Runner")).toHaveValue("e2e-sleeper");
  await page.getByRole("button", { name: /enable dispatch/i }).click();
  await expect(settings.getByText("This project", { exact: true }).locator("..")).toContainText(
    "Open",
  );
  await expect(settings).toContainText("runner: e2e-sleeper");

  // A task whose ball is with an agent, created by a human -- which is what makes it
  // dispatchable at all, since a dispatch may only follow a human's log entry.
  await page.getByRole("link", { name: "Create", exact: true }).click();
  await page.getByRole("textbox", { name: "Title", exact: true }).fill("Dispatch me");
  await page.getByRole("textbox", { name: /^Summary/ }).fill("Started from the browser.");
  await page
    .getByRole("textbox", { name: /^Working description/ })
    .fill("Proves a click in the browser starts a process on this machine.");
  await page.getByRole("radio", { name: /^Ready/ }).check();
  await page.getByRole("button", { name: "Create task" }).click();

  const tasks = page.getByRole("region", { name: "Tasks" });
  await tasks.getByText("Dispatch me").click();

  const dispatch = page.getByRole("region", { name: "Dispatch" });
  await expect(dispatch).toContainText("This is not approval");
  await expect(dispatch).toContainText("e2e-sleeper");

  await dispatch.getByRole("button", { name: /dispatch/i }).click();

  // A live run, reported without the page being reloaded.
  const run = dispatch.getByRole("listitem").first();
  await expect(run).toContainText("Running", { timeout: 15_000 });
  await expect(run).toContainText("Running for");
  await expect(run.getByRole("link", { name: /view output/i })).toHaveAttribute(
    "href",
    /\/api\/projects\/_local\/dispatch\/runs\/run_[0-9a-f]+\/output$/,
  );

  await run.getByRole("button", { name: /cancel run/i }).click();

  // Cancelled, and reported as cancelled -- not as "failed", which is what the run's
  // own supervisor would call a killed process if it won the race to write first.
  await expect(run).toContainText("Cancelled", { timeout: 15_000 });
  await expect(run.getByRole("button", { name: /cancel run/i })).toHaveCount(0);
  await expect(run).toContainText("Ran for");

  // The run's end landed on the task record, which is the point of the whole thing.
  await expect(page.getByRole("region", { name: "Task log" })).toContainText("dispatch_result");

  // Cancelling hands the ball to a human, which is correct and also means this task
  // would sit in every later spec's dashboard as the project's next action -- these
  // specs share one server and one project. Archiving it is both the cleanup and one
  // more real flow exercised.
  await page
    .getByRole("region", { name: "Review actions" })
    .getByRole("button", { name: /reject/i })
    .click();
  await page.getByLabel("Reason for rejection").fill("End-to-end run finished with it.");
  await page.getByRole("button", { name: "Submit" }).click();
  await expect(page).toHaveURL(/\/app\/p\/_local\/tasks$/);

  await page.getByRole("link", { name: "Dispatch", exact: true }).click();
  await page.getByRole("button", { name: /disable dispatch/i }).click();
  await expect(settings.getByText("This project", { exact: true }).locator("..")).toContainText(
    "Closed",
  );
  await expect(page.getByRole("button", { name: /enable dispatch/i })).toBeVisible();
});
