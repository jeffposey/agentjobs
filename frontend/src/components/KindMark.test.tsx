import { fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import type { TaskDetailResponse, TaskRead } from "../api/types";
import { KindMark, kindName } from "./KindMark";
import { TaskDetail } from "./TaskDetail";
import { TaskList } from "./TaskList";

/**
 * A task's kind, on every surface task-593 puts it: the list, the filter, the record
 * header and the review panel. Every assertion reads rendered text or an attribute a
 * browser acts on -- the word, the `data-kind` value, the URL -- never the mere presence
 * of an element.
 */

function task(id: string, overrides: Partial<TaskRead> = {}): TaskRead {
  return {
    schema: 2,
    id,
    title: `Title of ${id}`,
    created: "2026-09-25T08:00:00Z",
    updated: "2026-09-25T09:00:00Z",
    lifecycle: "ready",
    ball: "agent",
    ball_reason: "available",
    display_status: "Ready",
    status_category: "ready",
    priority: "high",
    category: "ux",
    tags: [],
    assignment: { eligible: [] },
    spec: { summary: `Summary of ${id}`, description: "Body." },
    ...overrides,
  };
}

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="location">{location.pathname}{location.search}</output>;
}

const design = task("task-design", { kind: "design", title: "Decide the shape" });
const build = task("task-build", { title: "Build the shape" });

describe("KindMark", () => {
  it("draws the word for both kinds in a solid, rounded outline, never colour alone", () => {
    render(<><KindMark kind="design" /><KindMark kind={null} /></>);
    const marks = [...document.querySelectorAll<HTMLElement>("[data-kind]")];
    expect(marks.map((mark) => [mark.getAttribute("data-kind"), mark.textContent])).toEqual([
      ["design", "Design"],
      ["implementation", "Implementation"],
    ]);
    // The owner's review: the word alone, no icon beside it.
    for (const mark of marks) {
      expect(mark.querySelector("svg")).toBeNull();
      // A dashed line was too hard to read (owner's second review).
      expect(mark).not.toHaveClass("border-dashed");
      expect(mark).toHaveClass("rounded-full", "border", "font-semibold");
    }
    // Design is the Draft chip's yellow, as an outline over a tint; never the landing's violet.
    const designMark = marks[0] as HTMLElement;
    expect(designMark.style.color).toBe("rgb(253, 224, 71)");
    expect(designMark.style.borderColor).toBe("rgb(250, 204, 21)");
    expect(designMark.style.backgroundColor).toBe("rgba(250, 204, 21, 0.12)");
  });

  it("reads an absent or unknown kind as implementation", () => {
    expect(kindName(undefined)).toBe("implementation");
    expect(kindName("research")).toBe("implementation");
    expect(kindName("design")).toBe("design");
  });
});

describe("the list marks only design rows", () => {
  it.each(["table", "tree"] as const)("in the %s variant", (variant) => {
    render(
      <MemoryRouter initialEntries={["/p/inbox/tasks"]}>
        <TaskList tasks={[design, build]} projectId="inbox" variant={variant} reorder={null} />
      </MemoryRouter>,
    );
    const rowOf = (id: string) => document.querySelector(`[data-task="${id}"]`) as HTMLElement;
    expect(within(rowOf("task-design")).getByText("Design").closest("[data-kind]")).toHaveAttribute(
      "data-kind",
      "design",
    );
    expect(rowOf("task-build").querySelector("[data-kind]")).toBeNull();
    expect(within(rowOf("task-build")).queryByText("Implementation")).not.toBeInTheDocument();
  });
});

describe("the kind filter", () => {
  function renderList(entry = "/p/inbox/tasks") {
    render(
      <MemoryRouter initialEntries={[entry]}>
        <TaskList tasks={[design, build]} projectId="inbox" />
        <LocationProbe />
      </MemoryRouter>,
    );
  }
  const visibleIds = () =>
    [...document.querySelectorAll("[data-task]")].map((row) => row.getAttribute("data-task"));

  it("narrows the list, writes ?kind= and is counted in the badge", () => {
    renderList();
    expect(visibleIds()).toEqual(["task-design", "task-build"]);

    fireEvent.click(screen.getByRole("button", { name: /^Filters/ }));
    const popover = screen.getByRole("dialog", { name: "Filters" });
    fireEvent.change(within(popover).getByRole("combobox", { name: "Kind" }), {
      target: { value: "design" },
    });

    expect(visibleIds()).toEqual(["task-design"]);
    expect(screen.getByTestId("location")).toHaveTextContent("kind=design");
    expect(screen.getByTestId("active-filter-count")).toHaveTextContent("1");
    expect(screen.getByRole("button", { name: /^Filters/ })).toHaveAccessibleName(
      "Filters, 1 set: Kind design",
    );
  });

  it("treats a task with no kind as implementation, from a pasted URL", () => {
    renderList("/p/inbox/tasks?kind=implementation");
    expect(visibleIds()).toEqual(["task-build"]);
    expect(screen.getByTestId("active-filter-count")).toHaveTextContent("1");
  });

  it("is cleared with the others", () => {
    renderList("/p/inbox/tasks?kind=design");
    fireEvent.click(screen.getByRole("button", { name: /^Filters/ }));
    fireEvent.click(screen.getByRole("button", { name: "Clear all filters" }));
    expect(screen.getByTestId("location")).not.toHaveTextContent("kind=");
    expect(visibleIds()).toEqual(["task-design", "task-build"]);
  });
});

function detailOf(record: TaskRead, relations: Partial<TaskDetailResponse> = {}): TaskDetailResponse {
  return {
    task: record,
    parent_task: null,
    children: [],
    needs: [],
    blocks: [],
    related: [],
    child_dependency_edges: [],
    identity: { ok: true, user: "Jeff Posey", problem: null, detail: "" },
    ...relations,
  };
}

function renderDetail(value: TaskDetailResponse) {
  const done = vi.fn(async () => undefined);
  render(
    <MemoryRouter>
      <TaskDetail
        detail={value}
        projectId="inbox"
        onApprove={done}
        onSendBack={done}
        onReject={done}
        onPromote={done}
        onResume={done}
        onAddNote={done}
        onSaveFields={done}
      />
    </MemoryRouter>,
  );
}

const headerKind = () => document.querySelector("header [data-kind]");

describe("the record header names the kind", () => {
  it("names Design, and what is waiting on it", () => {
    renderDetail(
      detailOf(design, {
        blocks: [
          { task_id: "task-build", title: "Build the shape", kind: null, exists: true, state: "open", note: null, reason: "task-build needs this task." },
        ],
      }),
    );
    expect(headerKind()).toHaveAttribute("data-kind", "design");
    expect(headerKind()).toHaveTextContent("Design");
    const line = document.querySelector('[data-field="implemented-by"]');
    expect(line).toHaveTextContent("Implemented by: task-build (1 waiting on this)");
    expect(within(line as HTMLElement).getByRole("link", { name: "task-build" })).toHaveAttribute(
      "href",
      "/p/inbox/tasks/task-build",
    );
  });

  it("names Implementation when kind is absent, and what it implements", () => {
    renderDetail(
      detailOf(build, {
        needs: [
          { task_id: "task-design", title: "Decide the shape", kind: "design", exists: true, state: "open", note: null, reason: "Needs task-design; it is still open." },
          { task_id: "task-plain", title: "A plain prerequisite", kind: null, exists: true, state: "done", note: null, reason: "Needs task-plain; it is closed as completed." },
        ],
      }),
    );
    expect(headerKind()).toHaveAttribute("data-kind", "implementation");
    expect(headerKind()).toHaveTextContent("Implementation");
    // Only the design prerequisite: a plain `needs` is not what this task implements.
    expect(document.querySelector('[data-field="implements"]')).toHaveTextContent(
      "Implements: task-design — Decide the shape",
    );
    expect(document.querySelector('[data-field="implemented-by"]')).toBeNull();
  });

  it("says neither when nothing links them", () => {
    renderDetail(detailOf(build));
    expect(document.querySelector('[data-field="implements"]')).toBeNull();
    expect(document.querySelector('[data-field="implemented-by"]')).toBeNull();
  });
});

describe("the review panel for a design", () => {
  const atReview = { lifecycle: "active" as const, ball: "human" as const, ball_reason: "review" as const, ball_prompt: "Review it.", display_status: "Needs review" };

  it("heads a design task's review as a design review, with the mark", () => {
    renderDetail(detailOf({ ...design, ...atReview }));
    const panel = screen.getByRole("region", { name: "Review actions" });
    const heading = within(panel).getByRole("heading", { level: 2 });
    expect(heading).toHaveTextContent("Design review — the ball is with you");
    expect(heading.querySelector("[data-kind]")).toHaveAttribute("data-kind", "design");
    // Copy only: the Approve button is task-001's and is unchanged.
    expect(within(panel).getByRole("button", { name: "✓ Approve — agent may merge" })).toBeVisible();
  });

  it("leaves an implementation task's panel as it was", () => {
    renderDetail(detailOf({ ...build, ...atReview }));
    const panel = screen.getByRole("region", { name: "Review actions" });
    const heading = within(panel).getByRole("heading", { level: 2 });
    expect(heading).toHaveTextContent(/^Needs review — the ball is with you$/);
    expect(heading.querySelector("[data-kind]")).toBeNull();
    expect(within(panel).getByRole("button", { name: "✓ Approve — agent may merge" })).toBeVisible();
  });

  it("does not call a design's decision gate a design review", () => {
    renderDetail(detailOf({ ...design, ...atReview, ball_reason: "decision", display_status: "Needs decision" }));
    const heading = within(screen.getByRole("region", { name: "Review actions" })).getByRole("heading", { level: 2 });
    expect(heading).toHaveTextContent(/^Needs decision — the ball is with you$/);
  });
});
