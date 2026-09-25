import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { StatusCategory, TaskRead } from "../api/types";
import { DependencyState, dependencyState } from "./DependencyState";
import vocabulary from "../../../src/agentjobs/status_vocabulary.json";

function task(overrides: Partial<TaskRead> = {}): TaskRead {
  return {
    schema: 2,
    id: "task-562-fixture",
    title: "A task",
    created: "2026-09-24T08:00:00Z",
    updated: "2026-09-24T09:00:00Z",
    lifecycle: "ready",
    ball: "agent",
    ball_reason: "available",
    outcome: null,
    display_status: "Ready",
    status_category: "ready",
    priority: "medium",
    category: "ux",
    tags: [],
    assignment: { eligible: [] },
    spec: { summary: "Summary.", description: "Body." },
    ...overrides,
  };
}

/** The chip a browser receives: its text, its category and its computed colours. */
function chip(): HTMLElement {
  const element = document.querySelector<HTMLElement>("[data-status-category]");
  if (!element) throw new Error("no status chip rendered");
  return element;
}

type Colours = { fill: string; border: string; text: string; border_style?: string };
const CATEGORIES = vocabulary.categories as Record<StatusCategory, Colours>;

/**
 * What the data file says a category looks like, normalised the way the browser
 * normalises the chip's own inline style, so the two compare as rendered values.
 */
function expectedStyle(category: StatusCategory): string {
  const colours = CATEGORIES[category];
  const probe = document.createElement("span");
  probe.style.backgroundColor = colours.fill;
  probe.style.borderColor = colours.border;
  probe.style.borderStyle = colours.border_style ?? "solid";
  probe.style.color = colours.text;
  return probe.style.cssText;
}

/**
 * The design table (task-562, as revised by the owner on 2026-09-24), one row per state.
 * The server sends the word and the category; what is asserted is what the browser
 * renders from them -- the text, the category, and the exact colours -- per the
 * rendered-value rule.
 */
const TABLE: Array<[string, StatusCategory, Partial<TaskRead>]> = [
  ["Ready", "ready", {}],
  ["Queued", "queued", {}],
  ["Starting", "queued", {}],
  ["Working", "working", { lifecycle: "active", ball_reason: "work", ball_prompt: "Go." }],
  ["Walking", "working", { lifecycle: "active", ball_reason: "work", ball_prompt: "Go." }],
  ["Landing", "finishing", { lifecycle: "active", ball_reason: "work", ball_prompt: "Go." }],
  ["Needs spec", "needs_you", { ball: "human", ball_reason: "spec", ball_prompt: "Spec it." }],
  ["Needs review", "needs_you", { ball: "human", ball_reason: "review", ball_prompt: "Look." }],
  ["Needs plan approval", "needs_you", { ball: "human", ball_reason: "plan", ball_prompt: "Plan." }],
  ["Needs decision", "needs_you", { ball: "human", ball_reason: "decision", ball_prompt: "Pick." }],
  ["Needs approval", "needs_you", { ball: "human", ball_reason: "approval", ball_prompt: "OK?" }],
  ["Needs input", "needs_you", { ball: "human", ball_reason: "input", ball_prompt: "Say." }],
  ["Error", "needs_you", { needs_cycles: [["task-1", "task-2", "task-1"]] }],
  ["Blocked", "not_now", { unmet_needs: ["task-042"] }],
  ["On hold", "not_now", { lifecycle: "active", ball_reason: "hold", ball_prompt: "Held." }],
  ["Quota", "not_now", { lifecycle: "active", ball: "external", ball_reason: "service", ball_prompt: "Limit." }],
  ["Draft", "draft", { lifecycle: "draft" }],
  ["Completed", "closed", { lifecycle: "closed", ball: null, ball_reason: null, outcome: "completed" }],
  ["Superseded", "closed_unfinished", { lifecycle: "closed", ball: null, ball_reason: null, outcome: "superseded" }],
  ["Cancelled", "closed_unfinished", { lifecycle: "closed", ball: null, ball_reason: null, outcome: "cancelled" }],
  ["Duplicate", "closed_unfinished", { lifecycle: "closed", ball: null, ball_reason: null, outcome: "duplicate" }],
];

describe("the status chip renders the design table", () => {
  it.each(TABLE)("%s is drawn as %s", (label, category, overrides) => {
    render(<DependencyState task={task({ ...overrides, display_status: label, status_category: category })} />);

    expect(chip()).toHaveTextContent(label);
    expect(chip()).toHaveAttribute("data-status-category", category);
    expect(chip().style.cssText).toBe(expectedStyle(category));
  });

  it("covers every category the data file defines", () => {
    expect(new Set(TABLE.map(([, category]) => category))).toEqual(new Set(Object.keys(CATEGORIES)));
  });

  it("uses every label the data file gives a task", () => {
    const labels = Object.values(vocabulary.statuses).map((entry) => entry.label);
    expect(new Set(TABLE.map(([label]) => label))).toEqual(new Set(labels));
  });

  it("never strikes a word through", () => {
    // The owner rejected the strikethrough (2026-09-24); an unfinished ending is hollow.
    for (const [label, category, overrides] of TABLE) {
      const { unmount } = render(
        <DependencyState task={task({ ...overrides, display_status: label, status_category: category })} />,
      );
      expect(chip().className, label).not.toMatch(/line-through/);
      unmount();
    }
  });

  it("draws Completed solid and the unfinished endings hollow", () => {
    render(<DependencyState task={task({ ...TABLE[15]![2], display_status: "Completed", status_category: "closed" })} />);
    const completed = chip().style.backgroundColor;
    expect(completed).not.toBe("transparent");
    document.body.innerHTML = "";

    render(<DependencyState task={task({ ...TABLE[16]![2], display_status: "Superseded", status_category: "closed_unfinished" })} />);
    expect(chip().style.backgroundColor).toBe("transparent");
    expect(chip().style.borderStyle).toBe("dashed");
  });
});

describe("the chip says the server's word and never its own", () => {
  // Before task-562 this component rewrote "Ready" to "Actionable now", an agent's
  // label to "In flight", and a parent's to "Waiting on sub-tasks".
  it.each([
    [{ actionable: true }, "Ready"],
    [{ lifecycle: "active" as const, ball_reason: "work" as const, ball_prompt: "Go." }, "Working"],
    [{ unmet_needs: ["task-9"] }, "Blocked"],
  ])("renders display_status as sent", (overrides, label) => {
    render(<DependencyState task={task({ ...overrides, display_status: label })} />);

    expect(chip()).toHaveTextContent(label);
    for (const retired of ["Actionable now", "In flight", "Waiting on sub-tasks"]) {
      expect(screen.queryByText(retired)).not.toBeInTheDocument();
    }
  });

  it("never reads Ready for a task that cannot start", () => {
    // The server's answer for a non-actionable ready task. The chip must not fall back
    // to a grey "Ready", which is what the old last branch did.
    render(
      <DependencyState
        task={task({ actionable: false, unmet_needs: ["task-1"], display_status: "Blocked", status_category: "not_now" })}
      />,
    );

    expect(chip()).not.toHaveTextContent("Ready");
    expect(chip()).not.toHaveAttribute("data-status-category", "ready");
  });

  it("capitalises whatever it is given", () => {
    render(<DependencyState task={task({ display_status: "queued", status_category: "queued" })} />);

    expect(chip().textContent).toBe("Queued");
  });

  it("shows the archived flag beside the chip, not in it", () => {
    render(
      <DependencyState
        task={task({ lifecycle: "closed", ball: null, ball_reason: null, outcome: "superseded", archived: true, display_status: "Superseded", status_category: "closed_unfinished" })}
      />,
    );

    expect(chip().textContent).toBe("Superseded");
    expect(screen.getByTestId("archived-tag")).toHaveTextContent("Archived");
  });
});

describe("the reason line", () => {
  it("names the blockers", () => {
    render(<DependencyState task={task({ unmet_needs: ["task-042", "task-043"], display_status: "Blocked", status_category: "not_now" })} />);

    expect(screen.getByText("Waiting for task-042")).toBeVisible();
    expect(screen.getByText("Waiting for task-043")).toBeVisible();
  });

  it("gives a quota reset its time, which left the chip", () => {
    const parked = task({
      lifecycle: "active",
      ball: "external",
      ball_reason: "service",
      ball_prompt: "The limit resets at 2026-09-18T21:30:00+00:00.",
      display_status: "Quota",
      status_category: "not_now",
      self_clearing_wait: { kind: "usage_limit", resets_at: "2026-09-18T21:30:00Z" },
    });
    const state = dependencyState(parked);

    expect(state.label).toBe("Quota");
    expect(state.reasons[0]).toMatch(/^Resumes by itself when the quota resets at /);
  });

  it("puts a service block's prompt under Blocked", () => {
    const blocked = task({
      lifecycle: "active",
      ball: "external",
      ball_reason: "service",
      ball_prompt: "GitHub is down.",
      display_status: "Blocked",
      status_category: "not_now",
    });

    expect(dependencyState(blocked).reasons).toEqual(["GitHub is down."]);
  });

  it("names the step a finish is on", () => {
    const finishing = task({
      lifecycle: "active",
      ball_reason: "work",
      ball_prompt: "Go.",
      display_status: "Landing",
      status_category: "finishing",
      live_finish: { finish_id: "f1", state: "running", current_step: "gate", step_meaning: "Running the gate" },
    });

    expect(dependencyState(finishing).reasons).toEqual(["Merging this branch. Running the gate."]);
  });

  it("says a queued dispatch is waiting for a slot", () => {
    const queued = task({
      display_status: "Queued",
      status_category: "queued",
      queued_dispatch: { queue_id: "q1", position: 1, queued_at: "2026-09-24T09:00:00Z" },
    });

    expect(dependencyState(queued).reasons).toEqual([
      "A dispatch of this task is waiting for a free slot. Nothing has started.",
    ]);
  });

  it("says what supervising a parent means", () => {
    // An epic nobody holds reads Ready (owner, 2026-09-24); the line says what claiming it gets.
    const parent = task({ open_children_count: 2, display_status: "Ready", status_category: "ready" });

    expect(dependencyState(parent).reasons[0]).toMatch(/^2 open sub-tasks to finish\./);
  });
});

describe("DependencyState in a list column", () => {
  const blocked = task({
    id: "task-341-fixture",
    unmet_needs: ["task-042", "task-043"],
    display_status: "Blocked",
    status_category: "not_now",
  });

  // A `ball_prompt` is written for someone who has the task open, so it is routinely
  // several paragraphs. Rendered one `<p>` per reason in a 194px table column it made
  // one row 2091px tall and emptied the first screen of the task list (task-341).
  it("puts every reason in one paragraph so the column can clamp it", () => {
    const { container } = render(<DependencyState task={blocked} compact />);

    const paragraphs = container.querySelectorAll("p");
    expect(paragraphs).toHaveLength(1);
    expect(paragraphs[0]).toHaveTextContent("Waiting for task-042 · Waiting for task-043");
  });

  it("keeps the full text reachable on the element that shows the clamp", () => {
    const { container } = render(<DependencyState task={blocked} compact />);

    expect(container.querySelector("p")).toHaveAttribute(
      "title",
      "Waiting for task-042 · Waiting for task-043",
    );
  });

  it("still gives each reason its own paragraph when it is not compact", () => {
    const { container } = render(<DependencyState task={blocked} />);

    expect(container.querySelectorAll("p")).toHaveLength(2);
    expect(container.querySelector("p")).not.toHaveAttribute("title");
  });

  it("renders no paragraph at all when there is nothing to say", () => {
    const { container } = render(<DependencyState task={task({ actionable: true })} compact />);

    expect(container.querySelectorAll("p")).toHaveLength(0);
    expect(chip()).toHaveTextContent("Ready");
  });
});

describe("a response from an older server", () => {
  it("draws an empty chip rather than taking the page down when the word is missing", () => {
    const older = task({ display_status: undefined as unknown as string });

    expect(() => render(<DependencyState task={older} />)).not.toThrow();
    expect(chip().textContent).toBe("");
  });
});

describe("a task chip moves only when a live fact backs it (task-570)", () => {
  const working = { lifecycle: "active", ball_reason: "work", display_status: "Working", status_category: "working" } as const;

  it("orbits a Working task whose run is producing output", () => {
    render(<DependencyState task={task({ ...working, live_run_health: "working" })} />);
    expect(chip()).toHaveAttribute("data-motion", "orbit");
  });

  it("keeps a Working task still when no live run backs it", () => {
    render(<DependencyState task={task(working)} />);
    expect(chip()).not.toHaveAttribute("data-motion");
    expect(chip().className).not.toContain("chip-motion");
  });

  it("keeps a Working task still when its run is parked", () => {
    render(<DependencyState task={task({ ...working, live_run_health: "parked" })} />);
    expect(chip()).not.toHaveAttribute("data-motion");
  });
  it("flashes a task waiting on a person (task-577)", () => {
    render(
      <DependencyState
        task={task({ lifecycle: "active", ball: "human", ball_reason: "review", display_status: "Needs review", status_category: "needs_you" })}
      />,
    );
    expect(chip()).toHaveAttribute("data-motion", "flash");
    expect(chip()).toHaveClass("chip-motion-flash");
  });
});
