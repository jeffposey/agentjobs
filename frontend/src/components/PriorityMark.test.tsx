import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { PriorityMark } from "./PriorityMark";
import { StatusChip } from "./StatusChip";

/**
 * A priority must be told from a status by form, not colour (task-563): no box, a bar
 * count that is the priority, and uppercase where status words are sentence case.
 */
describe("PriorityMark", () => {
  it.each([
    ["critical", "CRITICAL", 4],
    ["high", "HIGH", 3],
    ["medium", "MEDIUM", 2],
    ["low", "LOW", 1],
  ])("draws %s as %s with %i filled bars", (priority, word, filled) => {
    render(<PriorityMark priority={priority} />);
    const mark = screen.getByText(word).parentElement as HTMLElement;
    const bars = Array.from(mark.querySelectorAll("rect"));
    expect(bars).toHaveLength(4);
    expect(bars.filter((bar) => bar.getAttribute("opacity") === "1")).toHaveLength(filled);
    expect(mark).toHaveAttribute("title", `Priority: ${priority}`);
  });

  it("draws an absent or unknown priority as medium rather than failing", () => {
    render(
      <>
        <PriorityMark priority={null} />
        <PriorityMark priority="someday" />
      </>,
    );
    expect(screen.getAllByText("MEDIUM")).toHaveLength(2);
  });

  it("has no box where a status chip has one", () => {
    render(
      <>
        <StatusChip category="needs_you" label="Blocked" testId="chip" />
        <PriorityMark priority="critical" />
      </>,
    );
    const chip = screen.getByTestId("chip");
    const mark = screen.getByText("CRITICAL").parentElement as HTMLElement;
    expect(chip.className).toMatch(/\bborder\b/);
    expect(chip.style.backgroundColor).not.toBe("");
    expect(mark.className).not.toMatch(/\bborder\b|\brounded\b|\bbg-/);
    expect(mark.style.backgroundColor).toBe("");
    // Sentence case against uppercase: the two vocabularies differ in case too.
    expect(chip).toHaveTextContent("Blocked");
  });

  it("extends the word for a band header", () => {
    render(<PriorityMark priority="high" suffix=" TASKS" />);
    expect(screen.getByText("HIGH TASKS")).toBeInTheDocument();
  });
});
