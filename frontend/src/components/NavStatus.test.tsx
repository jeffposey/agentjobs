import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import vocabulary from "../../../src/agentjobs/status_vocabulary.json";

import { NavCounts, navStatusLabel } from "./NavStatus";

/**
 * task-588 and task-608: the Dashboard tab's three counts -- waiting on you, being
 * worked, landing.
 *
 * Assertions are on the rendered numbers and the rendered colours, which are what a
 * person reads. Which runs count as working and which as landing is LiveRuns.test.tsx's;
 * where the counts go -- inside the one Dashboard link -- is PrimaryNav.test.tsx's; that
 * they fit a phone is measured in a browser, in e2e/attention-badge.spec.ts.
 */

const count = (id: string) => screen.getByTestId(id).getAttribute("data-count");

/** The vocabulary's colour as the browser reports an inline style, `rgb(r, g, b)`. */
function rgb(hex: string): string {
  const n = parseInt(hex.slice(1), 16);
  return `rgb(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255})`;
}

describe("NavCounts", () => {
  it("shows waiting, working and landing, in that order, each number inside its dot", () => {
    const { container } = render(<NavCounts waiting={1} working={2} landing={3} />);
    const dots = Array.from(container.querySelectorAll("[data-testid^='nav-status-']"));
    expect(dots.map((dot) => dot.getAttribute("data-testid"))).toEqual([
      "nav-status-waiting",
      "nav-status-working",
      "nav-status-landing",
    ]);
    // The number is the dot's own text, not a sibling beside it.
    expect(dots.map((dot) => dot.textContent)).toEqual(["1", "2", "3"]);
    expect(dots.every((dot) => dot.children.length === 0)).toBe(true);
    expect(count("nav-status-landing")).toBe("3");
  });

  it("draws each lit dot in its status vocabulary colour", () => {
    render(<NavCounts waiting={1} working={1} landing={1} />);
    const expected = [
      ["nav-status-waiting", "needs_you"],
      ["nav-status-working", "working"],
      ["nav-status-landing", "finishing"],
    ] as const;
    for (const [id, category] of expected) {
      const dot = screen.getByTestId(id);
      const colours = vocabulary.categories[category];
      expect(dot).toHaveAttribute("data-status-category", category);
      expect(dot.style.backgroundColor).toBe(rgb(colours.fill));
      expect(dot.style.borderColor).toBe(rgb(colours.border));
      expect(dot.style.color).toBe(rgb(colours.text));
    }
    // The named families, so a vocabulary edit that swapped two of them is caught here
    // rather than read as a pass: red, the working blue, the finishing violet.
    expect(vocabulary.categories.needs_you.border).toBe("#dc2626");
    expect(vocabulary.categories.working.border).toBe("#2563eb");
    expect(vocabulary.categories.finishing.border).toBe("#7c3aed");
  });

  it("draws a zero as a zero, muted and uncoloured", () => {
    // A part that vanished would look the same as one that had not loaded.
    render(<NavCounts waiting={0} working={0} landing={0} />);
    for (const id of ["nav-status-waiting", "nav-status-working", "nav-status-landing"]) {
      const dot = screen.getByTestId(id);
      expect(dot).toHaveTextContent("0");
      expect(dot).not.toHaveAttribute("data-status-category");
      expect(dot.style.backgroundColor).toBe("");
      expect(dot).toHaveClass("bg-slate-700", "text-slate-300");
    }
  });

  it("reads zero waiting before the attention answer arrives", () => {
    render(<NavCounts waiting={null} working={0} landing={0} />);
    expect(count("nav-status-waiting")).toBe("0");
  });

  it("caps every number inside its dot so the tab stops changing width", () => {
    render(<NavCounts waiting={14} working={10} landing={12} />);
    expect(screen.getByTestId("nav-status-waiting")).toHaveTextContent(/^9\+$/);
    expect(screen.getByTestId("nav-status-working")).toHaveTextContent(/^9\+$/);
    expect(screen.getByTestId("nav-status-landing")).toHaveTextContent(/^9\+$/);
    expect(count("nav-status-landing")).toBe("12");
  });

  it("has no slot fraction and no link of its own", () => {
    // The owner rejected both on review: the counts are content inside the Dashboard
    // link, and `2/3` was a number nobody reads.
    const { container } = render(<NavCounts waiting={2} working={3} landing={1} />);
    expect(container.querySelector("a")).toBeNull();
    expect(container).not.toHaveTextContent("/");
  });
});

describe("navStatusLabel", () => {
  it("spells all three counts out, with the real number past the cap", () => {
    expect(navStatusLabel(14, 2, 1)).toBe("14 waiting on you · 2 being worked · 1 landing");
    expect(navStatusLabel(null, 0, 0)).toBe("0 waiting on you · 0 being worked · 0 landing");
  });
});
