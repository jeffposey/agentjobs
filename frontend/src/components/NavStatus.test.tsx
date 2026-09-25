import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { NavCounts, navStatusLabel } from "./NavStatus";

/**
 * task-588: the Dashboard tab's two counts -- waiting on you, being worked.
 *
 * Assertions are on the rendered numbers, which are what a person reads. Where the
 * counts go -- inside the one Dashboard link -- is PrimaryNav.test.tsx's; that they fit
 * a phone is measured in a browser, in e2e/attention-badge.spec.ts.
 */

const count = (id: string) => screen.getByTestId(id).getAttribute("data-count");

describe("NavCounts", () => {
  it("shows waiting on you and being worked", () => {
    render(<NavCounts waiting={1} working={2} />);
    expect(count("nav-status-waiting")).toBe("1");
    expect(count("nav-status-working")).toBe("2");
    expect(screen.getByTestId("nav-status-waiting")).toHaveTextContent("1");
    expect(screen.getByTestId("nav-status-working")).toHaveTextContent("2");
  });

  it("draws a zero as a zero, not as nothing", () => {
    // A part that vanished would look the same as one that had not loaded.
    render(<NavCounts waiting={0} working={0} />);
    expect(screen.getByTestId("nav-status-waiting")).toHaveTextContent("0");
    expect(screen.getByTestId("nav-status-working")).toHaveTextContent("0");
  });

  it("reads zero waiting before the attention answer arrives", () => {
    render(<NavCounts waiting={null} working={0} />);
    expect(count("nav-status-waiting")).toBe("0");
  });

  it("caps the number so the tab stops changing width", () => {
    render(<NavCounts waiting={14} working={1} />);
    expect(screen.getByTestId("nav-status-waiting")).toHaveTextContent("9+");
  });

  it("has no slot fraction and no link of its own", () => {
    // The owner rejected both on review: the counts are content inside the Dashboard
    // link, and `2/3` was a number nobody reads.
    const { container } = render(<NavCounts waiting={2} working={3} />);
    expect(container.querySelector("a")).toBeNull();
    expect(container).not.toHaveTextContent("/");
  });
});

describe("navStatusLabel", () => {
  it("spells both counts out, with the real number past the cap", () => {
    expect(navStatusLabel(14, 2)).toBe("14 waiting on you · 2 being worked");
    expect(navStatusLabel(null, 0)).toBe("0 waiting on you · 0 being worked");
  });
});
