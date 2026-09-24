import { expect, test, type APIRequestContext } from "./fixtures";

/**
 * task-576: the sidebar's visual weight after task-563.
 *
 * Three claims, each a computed value rather than a class, because a class is present
 * whether or not Tailwind generated a rule for it (an arbitrary value such as
 * `border-l-[3px]` only exists if the build saw it). ENGINEERING.md, Verification.
 *
 *  - every task row's band edge is 3px, not 4px;
 *  - a band header has no edge, and its text is inset by the edge's width anyway, so it
 *    sits where it sat when it had one;
 *  - a sidebar title is weight 400 in the same colour as before, while the detail
 *    panel's title stays the bold headline.
 */

/** Tall and wide enough for the two-region shell, so the list is the sidebar. */
const TWO_REGION = { width: 1280, height: 800 };

/** Narrows the shared corpus to this spec's own rows. */
const TOKEN = "gh576weight";

async function seed(request: APIRequestContext) {
  const created: Array<string> = [];
  for (const priority of ["critical", "high"]) {
    const response = await request.post("/api/tasks", {
      data: {
        title: `${TOKEN} ${priority} row`,
        summary: "task-576 fixture.",
        description: "Exists so the sidebar has a band header and a row under it.",
        lifecycle: "ready",
        category: "ux",
        priority,
        actor: "E2E Human",
      },
    });
    expect(response.ok()).toBeTruthy();
    created.push((await response.json()).id as string);
  }
  return {
    ids: created,
    release: async () => {
      for (const id of created) {
        const closed = await request.post(`/api/tasks/${id}/close`, {
          data: { actor: "E2E Human", outcome: "cancelled", body: "Layout fixture." },
        });
        expect(closed.ok()).toBeTruthy();
      }
    },
  };
}

test.describe("the sidebar's edges and title weight", () => {
  let fixtures: Awaited<ReturnType<typeof seed>>;
  let api: APIRequestContext;

  test.beforeAll(async ({ playwright, serverURL }) => {
    api = await playwright.request.newContext({ baseURL: serverURL });
    fixtures = await seed(api);
  });

  test.afterAll(async () => {
    await fixtures.release();
    await api.dispose();
  });

  test("rows have a 3px edge, headers none, and titles are normal weight", async ({ page }) => {
    await page.setViewportSize(TWO_REGION);
    const [critical] = fixtures.ids;
    await page.goto(`/app/p/_local/tasks/${critical}?q=${TOKEN}`);
    await expect(page.locator(`[data-task="${critical}"]`)).toBeVisible();
    await expect(page.locator("h1", { hasText: `${TOKEN} critical row` })).toBeVisible();

    const measured = await page.evaluate(() => {
      const region = document.querySelector('[data-region="list"]');
      if (!region) throw new Error("No list region: this window did not get the sidebar.");
      const rows = [...region.querySelectorAll<HTMLElement>("li[data-task]")];
      const headers = [...region.querySelectorAll<HTMLElement>("li[data-band-header]")];
      const probe = document.createElement("span");
      probe.className = "text-dark-text";
      document.body.append(probe);
      const textColour = getComputedStyle(probe).color;
      probe.remove();
      return {
        rows: rows.map((row) => {
          const style = getComputedStyle(row);
          const title = row.querySelector<HTMLElement>('[data-field="title"]')!;
          return {
            edge: style.borderLeftWidth,
            edgeStyle: style.borderLeftStyle,
            weight: getComputedStyle(title).fontWeight,
            colour: getComputedStyle(title).color,
          };
        }),
        headers: headers.map((header) => {
          const style = getComputedStyle(header);
          const text = header.querySelector("h3")!;
          return {
            edge: style.borderLeftWidth,
            inset: Math.round(text.getBoundingClientRect().left - header.getBoundingClientRect().left),
          };
        }),
        textColour,
        detailWeight: getComputedStyle(document.querySelector("h1")!).fontWeight,
      };
    });

    expect(measured.rows.length).toBeGreaterThanOrEqual(2);
    for (const row of measured.rows) {
      expect(row.edge).toBe("3px");
      expect(row.edgeStyle).toBe("solid");
      expect(row.weight).toBe("400");
      // Lighter weight, not a dimmer colour: the title keeps the brightest text colour.
      expect(row.colour).toBe(measured.textColour);
    }
    expect(measured.headers.length).toBeGreaterThanOrEqual(2);
    for (const header of measured.headers) {
      expect(header.edge).toBe("0px");
      // The row's 3px edge plus the header's 0.5rem: where the text sat with an edge.
      expect(header.inset).toBe(11);
    }
    expect(measured.detailWeight).toBe("700");
  });
});
