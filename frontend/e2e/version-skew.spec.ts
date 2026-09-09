import { expect, test, type Page } from "@playwright/test";

/**
 * Version skew in a real browser, against the real server.
 *
 * The first test is the one that could not be written anywhere else. The digest is
 * computed in Python, from the live application, and stamped into the bundle by the
 * same script that writes `openapi.json`; the bundle compares the two in TypeScript.
 * Nothing in either language can tell you those two computations agree -- only a real
 * bundle talking to a real server can, and if they ever stopped agreeing every install
 * would carry a permanent banner and the feature would be switched off within a day.
 * A silent page here is that agreement.
 *
 * The second rewrites what the server answers, because the condition being detected is
 * by definition one the repository cannot be in: a checkout whose bundle and server
 * disagree fails `scripts/check.py` long before it gets here.
 */

/** Present on every project surface, so it stands for "the app is still working". */
function nav(page: Page) {
  return page.getByRole("navigation", { name: "Primary navigation" });
}

test("a page served by the server it was built from says nothing", async ({ page }) => {
  await page.goto("/app/p/_local");
  await expect(nav(page)).toBeVisible();

  // Give the first poll room to land and be wrong.
  await expect(page.getByText("disagree about the API")).toHaveCount(0);
  await expect(page.getByText("A newer build of AgentJobs")).toHaveCount(0);
});

test("a server serving a different contract is named, and the page still works", async ({
  page,
}) => {
  await page.route("**/api/version", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    // Same package version and same schema version, different contract: the 2026-08-17
    // incident, which no comparison of the other two identifiers can see.
    await route.fulfill({ json: { ...body, api_digest: "f".repeat(64) } });
  });

  await page.goto("/app/p/_local");

  const banner = page.getByRole("alert").filter({ hasText: "disagree about the API" });
  await expect(banner).toBeVisible();
  await expect(banner).toContainText("agentjobs restart");

  // Not a modal. The tracker is still a tracker while it tells you this.
  await expect(nav(page)).toBeVisible();
  await nav(page).getByRole("link", { name: "Tasks", exact: true }).click();
  await expect(page).toHaveURL(/\/tasks/);

  await banner.getByRole("button", { name: "Dismiss" }).click();
  await expect(banner).toHaveCount(0);
});
