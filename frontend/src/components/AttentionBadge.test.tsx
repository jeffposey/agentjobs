import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { ATTENTION_BADGE_MAX, AttentionBadge } from "./AttentionBadge";

/**
 * task-338: the header says when work has stopped on you, on every surface.
 *
 * Assertions are on the rendered number and the accessible name -- what a person
 * reads and what a screen reader says -- rather than on the presence of the span.
 */

function renderBadge(count: number | null) {
  return render(
    <MemoryRouter initialEntries={["/p/demo/tasks"]}>
      <AttentionBadge count={count} projectId="demo" />
    </MemoryRouter>,
  );
}

describe("AttentionBadge", () => {
  it("renders nothing at all when nothing is waiting", () => {
    // Not a zero. An alarm that is permanently on the screen stops being read, and
    // this one also spends width the bar has none of.
    const { container } = renderBadge(0);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing while the count is still unknown", () => {
    // The first paint of every page happens before the query resolves. A badge that
    // flashed a zero there would be a badge that flickers on every navigation.
    const { container } = renderBadge(null);
    expect(container).toBeEmptyDOMElement();
  });

  it("shows the count, and says what it means", () => {
    renderBadge(3);
    const badge = screen.getByRole("link", { name: "3 tasks are waiting on you" });
    expect(badge).toHaveTextContent("3");
  });

  it("gets the grammar right for a single task", () => {
    renderBadge(1);
    expect(screen.getByRole("link", { name: "1 task is waiting on you" })).toHaveTextContent("1");
  });

  it("caps the number so the pill stops changing width, and still says the real one", () => {
    renderBadge(14);
    const badge = screen.getByRole("link", { name: "14 tasks are waiting on you" });
    expect(badge).toHaveTextContent(`${ATTENTION_BADGE_MAX}+`);
  });

  it("leads to the dashboard of the project the bar is scoped to", () => {
    // The dashboard is where the same number is broken out into the tasks behind it,
    // computed from the same predicate -- so the badge and its destination agree.
    renderBadge(2);
    expect(screen.getByRole("link", { name: /waiting on you/ })).toHaveAttribute(
      "href",
      "/p/demo",
    );
  });
});
