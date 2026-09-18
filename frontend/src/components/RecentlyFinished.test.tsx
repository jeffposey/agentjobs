import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import type { ClosureView, RecentClosuresView } from "../api/types";
import { RecentlyFinished, closureAge } from "./RecentlyFinished";

/**
 * task-460: what landed while you were not looking.
 *
 * Every assertion here is on a **rendered value** rather than on the presence of markup,
 * which is ENGINEERING.md's rule and the reason it exists: a test that asserted a row
 * carries `data-outcome` would pass while the cell printed `Outcome.SUPERSEDED`, and the
 * outcome is precisely what a reader scans these rows for.
 */

function closure(overrides: Partial<ClosureView> = {}): ClosureView {
  return {
    task_id: "task-460",
    task_title: "A small recently-finished spot on the Dashboard",
    project_id: "agentjobs",
    project_name: "AgentJobs",
    outcome: "completed",
    closed_at: "2026-09-18T09:00:00+00:00",
    age_seconds: 7_200,
    task_url: "/p/agentjobs/tasks/task-460",
    ...overrides,
  };
}

function body(overrides: Partial<RecentClosuresView> = {}): RecentClosuresView {
  return {
    closures: [closure()],
    limit: 5,
    window_days: 7,
    generated_at: "2026-09-18T11:00:00+00:00",
    ...overrides,
  };
}

function draw(value: RecentClosuresView | null) {
  return render(
    <MemoryRouter>
      <RecentlyFinished body={value} projectId="agentjobs" />
    </MemoryRouter>,
  );
}

describe("a row", () => {
  it("prints the id, title, project, outcome and age a reader acts on", () => {
    draw(body());

    const row = screen.getByTestId("closure-row");
    expect(within(row).getByText("task-460")).toBeTruthy();
    expect(
      within(row).getByText("A small recently-finished spot on the Dashboard"),
    ).toBeTruthy();
    expect(within(row).getByText("AgentJobs")).toBeTruthy();
    expect(within(row).getByText("completed")).toBeTruthy();
    expect(within(row).getByText("2h ago")).toBeTruthy();
  });

  it("says a non-completed outcome rather than flattening it to done", () => {
    draw(body({ closures: [closure({ outcome: "superseded" })] }));

    expect(screen.getByText("superseded")).toBeTruthy();
    expect(screen.queryByText("completed")).toBeNull();
  });

  it("links into the row's own project, not the one being looked at", () => {
    draw(
      body({
        closures: [
          closure({
            task_id: "task-012",
            project_id: "mastercalls",
            project_name: "Mastercalls",
            task_url: "/p/mastercalls/tasks/task-012",
          }),
        ],
      }),
    );

    const link = screen.getByTestId("closure-row") as HTMLAnchorElement;
    expect(link.getAttribute("href")).toBe("/p/mastercalls/tasks/task-012");
  });

  it("carries the machine-readable instant beside the words", () => {
    // The words are relative and the attribute is absolute, so a reader hovering the
    // row -- or a tool reading the page -- gets the moment rather than an age.
    draw(body());

    const time = screen.getByTestId("closure-row").querySelector("time");
    expect(time?.getAttribute("datetime")).toBe("2026-09-18T09:00:00+00:00");
  });

  it("keeps the server's order", () => {
    draw(
      body({
        closures: [
          closure({ task_id: "task-003", age_seconds: 120 }),
          closure({ task_id: "task-002", age_seconds: 4_000 }),
          closure({ task_id: "task-001", age_seconds: 200_000 }),
        ],
      }),
    );

    const ids = screen.getAllByTestId("closure-row").map((row) => row.getAttribute("data-task"));
    expect(ids).toEqual(["task-003", "task-002", "task-001"]);
  });
});

describe("when nothing has finished", () => {
  it("says so, in the window the server actually used", () => {
    draw(body({ closures: [], window_days: 14 }));

    expect(screen.getByText("Nothing has finished in the last 14 days.")).toBeTruthy();
    expect(screen.queryByTestId("closure-row")).toBeNull();
  });

  it("does not say so before the answer has arrived", () => {
    // "Nothing finished this week" and "we have not been told yet" are different
    // sentences, and printing the first while the query is in flight is a claim the
    // page has no basis for.
    draw(null);

    expect(screen.getByText("Recently finished")).toBeTruthy();
    expect(screen.queryByText(/Nothing has finished/)).toBeNull();
  });
});

describe("the region", () => {
  it("offers the whole history it is a sample of", () => {
    draw(body());

    const link = screen.getByText("All closed →") as HTMLAnchorElement;
    expect(link.getAttribute("href")).toBe("/p/agentjobs/tasks?status=closed");
  });
});

describe("closureAge", () => {
  it.each([
    [0, "just now"],
    [59, "just now"],
    [60, "1m ago"],
    [3_540, "59m ago"],
    [3_600, "1h ago"],
    [86_399, "23h ago"],
    [86_400, "1d ago"],
    [604_800, "7d ago"],
  ])("renders %i seconds as %s", (seconds, expected) => {
    expect(closureAge(seconds)).toBe(expected);
  });

  it("never prints a negative age when the clocks disagree", () => {
    expect(closureAge(-5)).toBe("just now");
  });
});
