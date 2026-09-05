import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

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
    for (const label of [
      "Dashboard",
      "Tasks",
      "Create",
      "Dispatch",
      "Playbooks",
      "Runs",
      "API Docs",
    ]) {
      expect(within(opened as HTMLElement).getByText(label)).toBeInTheDocument();
    }
  });

  it("closes when a destination is chosen", () => {
    renderNav();
    fireEvent.click(trigger());
    fireEvent.click(within(panel() as HTMLElement).getByText("Dispatch"));
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

  it("puts the live-run badge on the Runs entry and nowhere else", () => {
    // The badge is a node the shell supplies, so this asserts the wiring rather than
    // the count: which destination carries it, and that nothing else does.
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={["/p/demo"]}>
          <PrimaryNav projectId="demo" badge={<span data-testid="badge">7</span>} />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    const runs = screen.getByRole("link", { name: /Runs/ });
    expect(within(runs).getByTestId("badge")).toBeInTheDocument();
    expect(runs).toHaveAttribute("href", "/p/demo/runs");
    expect(screen.getAllByTestId("badge")).toHaveLength(1);
  });

  it("keeps the attention badge out of the collapsible group, so a phone still sees it", () => {
    // The load-bearing property of task-338, and the only half of it jsdom can see:
    // every destination in this bar disappears behind the burger below the
    // breakpoint, so a badge hung on one of them -- where the legacy Jinja header hung
    // it -- would be invisible on the surface that needs it most. Asserted as
    // structure, because jsdom applies no stylesheet and cannot be asked whether the
    // group is displayed. That it is genuinely visible at a phone width is measured in
    // e2e/attention-badge.spec.ts.
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={["/p/demo/tasks"]}>
          <PrimaryNav projectId="demo" attention={<span data-testid="attention">4</span>} />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    const attention = screen.getByTestId("attention");
    expect(attention.parentElement).toBe(
      screen.getByRole("navigation", { name: "Primary navigation" }),
    );
    // And it is there without opening anything.
    expect(panel()).toBeNull();
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
    // The case the whole longest-match rule exists for: `/tasks` matches this URL
    // too, and marking both would be no more use than marking neither.
    ["/p/demo/tasks/new", "/tasks/new"],
    ["/p/demo/dispatch", "/dispatch"],
    ["/p/demo/playbooks", "/playbooks"],
    ["/p/demo/runs", "/runs"],
  ])("marks %s as %s", (pathname, expected) => {
    expect(currentDestinationPath(pathname, "demo")).toBe(expected);
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
    ["/p/demo/tasks/new", "Create"],
    ["/p/demo/dispatch", "Dispatch"],
    ["/p/demo/playbooks", "Playbooks"],
    ["/p/demo/runs", "Runs"],
  ])("marks exactly one destination on %s, and it is %s", (at, label) => {
    renderNav(at);
    // Both halves matter. One, because the report was that nothing was marked; and
    // exactly one, because two marked entries answer the question no better.
    expect(current()).toHaveLength(1);
    expect(current()[0]).toHaveTextContent(label);
  });

  it("marks the current destination inside the burger panel too", () => {
    renderNav("/p/demo/dispatch");
    fireEvent.click(trigger());
    const opened = panel() as HTMLElement;
    const marked = within(opened).getAllByRole("link", { current: "page" });
    expect(marked).toHaveLength(1);
    expect(marked[0]).toHaveTextContent("Dispatch");
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

  it("gives Create no accent of its own, so colour in the bar means one thing", () => {
    // The report's actual cause: on the Dashboard, Create was the only coloured entry
    // in the bar and read as the selected tab. Regression guard, not tidiness.
    renderNav("/p/demo");
    const create = screen.getByRole("link", { name: "Create" });
    const tasks = screen.getByRole("link", { name: "Tasks" });
    expect(create.className).toBe(tasks.className);
  });
});
