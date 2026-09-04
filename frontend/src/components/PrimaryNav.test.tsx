import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { NAV_INLINE_MIN_PX, PrimaryNav } from "./PrimaryNav";
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

function renderNav() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/p/demo/tasks"]}>
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
