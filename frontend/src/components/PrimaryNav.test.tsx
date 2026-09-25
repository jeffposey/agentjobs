import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { setViewport } from "../test/viewport";
import { currentDestinationPath, NAV_INLINE_MIN_PX, PrimaryNav } from "./PrimaryNav";
// Vite hands the module back as text. Read through the bundler rather than through
// node:fs so the test needs no Node type definitions and no assumption about which
// directory the runner started in.
import navSource from "./PrimaryNav.tsx?raw";

/**
 * The panel's behaviour, which is the half of task-292 jsdom can see.
 *
 * The other half -- that the header is actually pinned, and that it is one row rather
 * than three -- is deliberately *not* asserted here. jsdom does not lay out, so every
 * `getBoundingClientRect()` is zero and `position: sticky` means nothing to it; a test
 * asserting `class="sticky"` would pass just as happily against a header nested in an
 * ancestor with `overflow: hidden`, where sticky silently does nothing. That evidence
 * is in `e2e/pinned-header.spec.ts`, measured in a real browser.
 */

function renderNav(at = "/p/demo/tasks") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[at]}>
        <PrimaryNav projectId="demo" />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const trigger = () => screen.getByRole("button", { name: "Navigation" });
const panel = () => document.getElementById("primary-nav-destinations");

/**
 * Everything the bar holds after task-345, in order.
 *
 * `Create` is gone, and that is task-346 rather than this task: its capture control
 * replaced the entry, so authoring stayed one interaction from every page instead of
 * becoming two. This task's own subtraction is Dispatch, Playbooks and API Docs, and
 * task-588's is Runs.
 */
const BAR_DESTINATIONS = ["Dashboard", "Tasks"] as const;

function renderWithStatus(at = "/p/demo/tasks", onDashboardFollow = () => {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[at]}>
        <PrimaryNav
          projectId="demo"
          status={<span data-testid="status">counts</span>}
          statusLabel="2 waiting on you · 3 being worked"
          onDashboardFollow={onDashboardFollow}
        />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("PrimaryNav", () => {
  it("starts closed, and says so to a screen reader", () => {
    renderNav();
    expect(trigger()).toHaveAttribute("aria-expanded", "false");
    expect(panel()).toBeNull();
  });

  it("opens every destination behind the burger, so nothing becomes unreachable", () => {
    renderNav();
    fireEvent.click(trigger());

    expect(trigger()).toHaveAttribute("aria-expanded", "true");
    const opened = panel();
    expect(opened).not.toBeNull();
    for (const label of BAR_DESTINATIONS) {
      expect(within(opened as HTMLElement).getByText(label)).toBeInTheDocument();
    }
  });

  it("carries navigation and nothing else", () => {
    // task-345. Asserted as the whole membership rather than as the presence of the
    // survivors, because the defect this task was filed against is *additions* nobody
    // subtracted: a test that only checks Dashboard is in the bar stays green through
    // another four years of that.
    renderNav();
    fireEvent.click(trigger());
    const labels = within(panel() as HTMLElement)
      .getAllByRole("link")
      .map((link) => link.textContent);
    expect(labels).toEqual([...BAR_DESTINATIONS]);
  });

  it("has no API Docs anchor anywhere in the bar", () => {
    // It moved to the actions menu, which is asserted from the other side in
    // ActionsMenu.test.tsx. Here the point is that the duplicate is gone: both
    // present was the state task-168 deliberately left behind for this task.
    renderNav();
    fireEvent.click(trigger());
    expect(screen.queryByRole("link", { name: "API Docs" })).toBeNull();
    expect(document.querySelector('header a[href="/docs"]')).toBeNull();
  });

  it("closes when a destination is chosen", () => {
    renderNav();
    fireEvent.click(trigger());
    fireEvent.click(within(panel() as HTMLElement).getByText("Tasks"));
    expect(panel()).toBeNull();
  });

  it("closes on Escape and hands focus back to the trigger", () => {
    renderNav();
    fireEvent.click(trigger());
    fireEvent.keyDown(document, { key: "Escape" });

    expect(panel()).toBeNull();
    // Without this the caret is left on a node that has just left the document, and a
    // keyboard user has to tab from the top of the page again.
    expect(document.activeElement).toBe(trigger());
  });

  it("closes when something outside the header is pressed", () => {
    renderNav();
    fireEvent.click(trigger());
    fireEvent.mouseDown(document.body);
    expect(panel()).toBeNull();
  });

  it("stays open when the press lands inside the header itself", () => {
    renderNav();
    fireEvent.click(trigger());
    fireEvent.mouseDown(screen.getByRole("heading", { name: "AgentJobs" }));
    expect(panel()).not.toBeNull();
  });

  it("has no Runs destination any more (task-588)", () => {
    renderNav();
    fireEvent.click(trigger());
    expect(screen.queryByRole("link", { name: /^Runs/ })).toBeNull();
    for (const link of screen.getAllByRole("link")) {
      expect(link.getAttribute("href") ?? "").not.toMatch(/\/runs$/);
    }
  });

  it("draws the counts inside the one Dashboard link when the row is inline", () => {
    // task-588, after review: the first cut put the counts in a pill link beside the
    // Dashboard link -- two links to one page. There is one link now, and the counts
    // are inside it. jsdom's default 1024px viewport is above the breakpoint.
    renderWithStatus();

    const dashboards = screen
      .getAllByRole("link")
      .filter((link) => link.getAttribute("href") === "/p/demo");
    expect(dashboards).toHaveLength(1);
    expect(dashboards[0]).toContainElement(screen.getByTestId("status"));
    expect(dashboards[0]).toHaveAccessibleName("Dashboard · 2 waiting on you · 3 being worked");
    expect(screen.getAllByTestId("status")).toHaveLength(1);
  });

  it("acknowledges through the Dashboard link that carries the counts", () => {
    // task-422's act, which the red badge's click carried before task-588.
    let followed = 0;
    renderWithStatus("/p/demo/tasks", () => {
      followed += 1;
    });
    fireEvent.click(screen.getByTestId("nav-status"));
    expect(followed).toBe(1);
    // The Tasks link is not that act.
    fireEvent.click(screen.getByRole("link", { name: "Tasks" }));
    expect(followed).toBe(1);
  });

  it("keeps the counts out of the collapsible group below the breakpoint", () => {
    // The load-bearing property of task-338, carried into task-588: every destination
    // disappears behind the burger below the breakpoint, so counts only inside the
    // Dashboard tab would be invisible on the phone, the surface that needs them most.
    // There the bar draws the Dashboard link as the counts alone. Asserted as structure,
    // because jsdom applies no stylesheet; that it is genuinely visible at a phone width
    // is measured in e2e/attention-badge.spec.ts.
    setViewport(390, 844);
    renderWithStatus();

    const link = screen.getByTestId("nav-status");
    expect(link.parentElement).toBe(screen.getByRole("navigation", { name: "Primary navigation" }));
    expect(link).toHaveAttribute("href", "/p/demo");
    expect(link).toContainElement(screen.getByTestId("status"));
    expect(panel()).toBeNull();
    // And opening the panel does not draw a second copy inside it.
    fireEvent.click(trigger());
    expect(within(panel() as HTMLElement).queryByTestId("status")).toBeNull();
    expect(screen.getAllByTestId("status")).toHaveLength(1);
  });

  it("keeps the burger's breakpoint and the class that hides it in agreement", () => {
    // Tailwind needs the pixel value as a literal inside the class name, so the
    // constant and the class cannot be derived from one another. Nothing but this
    // test would notice them drifting apart, and a drift means the burger and the
    // inline links are both visible -- or neither is -- in the band between them.
    const variants = [...navSource.matchAll(/min-\[(\d+)px\]:/g)].map((match) => Number(match[1]));
    expect(variants.length).toBeGreaterThan(0);
    expect(new Set(variants)).toEqual(new Set([NAV_INLINE_MIN_PX]));
  });
});

/**
 * task-336: the bar says which destination you are on.
 *
 * The rule is asserted here against URLs, and the rendering against `aria-current`.
 * That the marked entry actually *looks* different is not assertable in jsdom -- no
 * Tailwind stylesheet is loaded and `getComputedStyle` would report the same thing
 * for every link -- so that half is measured in a browser, in
 * `e2e/nav-current.spec.ts`.
 */
describe("currentDestinationPath", () => {
  it.each([
    ["/p/demo", ""],
    ["/p/demo/", ""],
    ["/p/demo/tasks", "/tasks"],
    ["/p/demo/tasks/task-042", "/tasks"],
    // The rule's answer with no deeper entry to find. `/tasks/new` had its own
    // destination until task-346 replaced Create with the capture control; longest
    // match now lands on `/tasks`, which is where creating a task belongs anyway.
    ["/p/demo/tasks/new", "/tasks"],
  ])("marks %s as %s", (pathname, expected) => {
    expect(currentDestinationPath(pathname, "demo")).toBe(expected);
  });

  it.each([["/p/demo/dispatch"], ["/p/demo/playbooks"], ["/p/demo/analytics"], ["/p/demo/runs"]])(
    "marks nothing on %s, rather than lighting up the Dashboard",
    (pathname) => {
      // ac-4, and the whole reason Dashboard's `""` stopped being a catch-all. These
      // two routes moved into the actions menu and no bar entry owns them any more;
      // under the old rule the bar would have told a reader standing on the dispatch
      // settings page that they were on the Dashboard, which is task-336's original
      // defect wearing a different hat and strictly worse than marking nothing.
      expect(currentDestinationPath(pathname, "demo")).toBeNull();
    },
  );

  it("marks nothing on a project route no entry has ever owned", () => {
    // Not a hypothetical: `/p/demo/*` renders a redirect, and any route added in
    // future arrives here before it arrives in DESTINATIONS. The rule has to be about
    // membership rather than about these two paths.
    expect(currentDestinationPath("/p/demo/something-new", "demo")).toBeNull();
  });

  it("matches the project at a segment boundary, so a prefix is not a project", () => {
    expect(currentDestinationPath("/p/demo2/tasks", "demo")).toBeNull();
    expect(currentDestinationPath("/not-found", "demo")).toBeNull();
  });

  it("reads the project id as it appears in the URL, encoded", () => {
    expect(currentDestinationPath("/p/my%20project/tasks", "my project")).toBe("/tasks");
  });
});

describe("PrimaryNav current destination", () => {
  const current = () => screen.queryAllByRole("link", { current: "page" });

  it.each([
    ["/p/demo", "Dashboard"],
    ["/p/demo/tasks", "Tasks"],
    ["/p/demo/tasks/task-042", "Tasks"],
    // No Create destination since task-346: the capture control replaced it, and
    // longest-match has nothing deeper than Tasks to offer this URL.
    ["/p/demo/tasks/new", "Tasks"],
  ])("marks exactly one destination on %s, and it is %s", (at, label) => {
    renderNav(at);
    // Both halves matter. One, because the report was that nothing was marked; and
    // exactly one, because two marked entries answer the question no better.
    expect(current()).toHaveLength(1);
    expect(current()[0]).toHaveTextContent(label);
  });

  it.each([["/p/demo/dispatch"], ["/p/demo/playbooks"], ["/p/demo/analytics"]])(
    "marks no entry at all on %s, which no destination owns",
    (at) => {
      // The rendered half of ac-4. The rule itself is covered above against URLs;
      // this is the bar actually drawn from it, including the burger panel, where a
      // wrongly-marked Dashboard would be just as misleading.
      renderNav(at);
      expect(current()).toHaveLength(0);
      fireEvent.click(trigger());
      expect(within(panel() as HTMLElement).queryAllByRole("link", { current: "page" })).toHaveLength(
        0,
      );
    },
  );

  it("marks the current destination inside the burger panel too", () => {
    renderNav("/p/demo/tasks");
    fireEvent.click(trigger());
    const opened = panel() as HTMLElement;
    const marked = within(opened).getAllByRole("link", { current: "page" });
    expect(marked).toHaveLength(1);
    expect(marked[0]).toHaveTextContent("Tasks");
  });

  it("styles the current destination differently from every other one", () => {
    // jsdom cannot see colour here, so this asserts the weaker thing it can: the
    // marked link is not handed the same class list as its neighbours. The colours
    // themselves are read out of a real browser in e2e/nav-current.spec.ts.
    renderNav("/p/demo/tasks");
    const links = screen.getAllByRole("link");
    const marked = links.filter((link) => link.getAttribute("aria-current") === "page");
    const rest = links.filter((link) => link.getAttribute("aria-current") !== "page");
    expect(marked).toHaveLength(1);
    expect(rest.length).toBeGreaterThan(0);
    const markedClass = (marked[0] as HTMLElement).className;
    for (const link of rest) {
      expect(link.className).not.toBe(markedClass);
    }
  });

  it("keeps the capture control out of the destinations, so colour still means one thing", () => {
    // The report's actual cause: on the Dashboard, Create was the only coloured entry
    // in the bar and read as the selected tab. task-346 removed that link and put the
    // act behind a button -- so the guard is now that the button is not a destination
    // and is not marked as one, whatever page you are on.
    renderNav("/p/demo");
    const capture = screen.getByRole("button", { name: "New task or issue" });
    expect(capture).not.toHaveAttribute("aria-current");
    expect(screen.queryByRole("link", { name: "Create" })).toBeNull();
    expect(current()).toHaveLength(1);
    expect(current()[0]).toHaveTextContent("Dashboard");
  });
});
