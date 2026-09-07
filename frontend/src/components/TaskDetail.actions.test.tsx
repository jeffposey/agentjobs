import { render } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import type { DispatchRunView, DispatchStateView, TaskDetailResponse, TaskRead } from "../api/types";
import { TaskDetail, type TaskDetailProps } from "./TaskDetail";

/**
 * Everything a reader can act on in the detail panel, listed rather than counted.
 *
 * task-239 fits this panel into a bounded region, and the failure that change makes
 * likely is the quiet one: a control that fell off the bottom of a narrower column, or
 * out of a header that became a pinned bar. Its acceptance criterion asks for the count
 * before and after -- so this file takes the count, and takes it as an *inventory*,
 * because a bare number that stays at 24 while Approve becomes a second Cancel is a
 * green test over a broken panel.
 *
 * The label is the accessible name a person reads, and every element that does anything
 * is in scope: buttons, links, the `<summary>` disclosures on long log entries, and the
 * form controls the composers open. Sorted, because the panel's *order* is asserted by
 * the tests next door; here only membership is the claim.
 *
 * This runs in jsdom, which has no layout, so it cannot see a control pushed off-screen
 * by a width. Nothing can: an element outside the viewport is still in the DOM. The
 * geometric half of the same question -- does the panel ever scroll sideways -- is
 * `e2e/task-detail-fit.spec.ts`, in a browser.
 */
function inventory(container: HTMLElement): Array<string> {
  const found: Array<string> = [];
  container
    .querySelectorAll<HTMLElement>("button, a[href], summary, input, textarea, select")
    .forEach((element) => {
      found.push(`${element.tagName.toLowerCase()}: ${name(element)}`);
    });
  return found.sort();
}

/** The name a person would use for this control, in the order a browser would resolve it. */
function name(element: HTMLElement): string {
  const aria = element.getAttribute("aria-label");
  if (aria) return aria;
  const labelled = element.getAttribute("id");
  if (labelled) {
    const label = element.ownerDocument.querySelector(`label[for="${labelled}"]`);
    if (label?.textContent?.trim()) return label.textContent.trim();
  }
  const own = element.closest("label");
  if (own && element.tagName !== "BUTTON" && own.textContent?.trim()) {
    return own.textContent.trim().replace(/\s+/g, " ");
  }
  const text = element.textContent?.trim().replace(/\s+/g, " ") ?? "";
  return text || `(${element.getAttribute("name") ?? element.getAttribute("type") ?? "unnamed"})`;
}

function task(id: string, overrides: Partial<TaskRead> = {}): TaskRead {
  return {
    schema: 2,
    id,
    title: `Title of ${id}`,
    created: "2026-08-13T08:00:00Z",
    updated: "2026-08-13T09:00:00Z",
    lifecycle: "active",
    ball: "human",
    ball_reason: "review",
    ball_prompt: "Read the entire record, then approve or request changes.",
    display_status: "Waiting for review",
    priority: "high",
    category: "ux",
    tags: ["react"],
    assignment: { owner: "codex", eligible: [] },
    spec: {
      summary: "Complete summary text.",
      intent: "Complete intent text.",
      description: "Complete working description text.",
      constraints: "Complete constraints text.",
      out_of_scope: "Complete out-of-scope text.",
      context: [{ path: "src/example.py", why: "Complete context reason." }],
    },
    acceptance: [{ id: "ac-1", text: "Complete acceptance text.", status: "pending" }],
    deliverables: [{ path: "src/example.py", note: "Changed.", status: "pending" }],
    links: [{ url: "https://example.invalid/spec", title: "The spec", rel: "doc" }],
    dependencies: [{ task: "task-needed", type: "needs", note: "Required first." }],
    log: [
      { id: 1, ts: "2026-08-13T08:00:00Z", actor: "codex", type: "question", body: "Still unanswered?" },
      { id: 2, ts: "2026-08-13T08:30:00Z", actor: "codex", type: "decision", body: "Use the typed detail contract." },
      { id: 3, ts: "2026-08-13T09:00:00Z", actor: "codex", type: "handoff", body: "Ready for review." },
    ],
    ...overrides,
  };
}

function detailFor(overrides: Partial<TaskRead> = {}): TaskDetailResponse {
  return {
    task: task("task-detail", { parent: "task-parent", ...overrides }),
    parent_task: task("task-parent", { title: "Parent title" }),
    children: [
      task("task-child", {
        ball: "agent",
        ball_reason: "available",
        ball_prompt: null,
        lifecycle: "ready",
        title: "Child title",
        display_status: "Ready",
      }),
    ],
    needs: [
      {
        task_id: "task-needed",
        title: "Required task",
        exists: true,
        state: "open",
        note: "Required first.",
        reason: "Needs task-needed; it is still open.",
      },
    ],
    blocks: [
      {
        task_id: "task-child",
        title: "Child title",
        exists: true,
        state: "open",
        note: null,
        reason: "task-child needs this task.",
      },
    ],
    related: [
      {
        task_id: "task-noticed-on",
        title: "The page it was noticed on",
        exists: true,
        state: "open",
        note: "Reported while viewing this task.",
        reason: "Related to task-noticed-on.",
      },
    ],
    child_dependency_edges: [
      {
        source: "task-missing",
        target: "task-child",
        note: "External gate.",
        source_exists: false,
        target_exists: true,
        source_contained: false,
        target_contained: true,
      },
    ],
    identity: { ok: true, user: "Jeff Posey", problem: null, detail: "" },
  };
}

const dispatchState: DispatchStateView = {
  project_id: "inbox",
  configured: true,
  master_enabled: true,
  sentinel_active: false,
  project_enabled: true,
  runner: "claude-session",
  posture: "supervised",
  auto_dispatch: false,
  available_runners: ["claude-session"],
  can_dispatch: true,
  refusal: null,
  config_path: "C:/Users/j/.agentjobs/dispatch.yaml",
  sentinel_file: "C:/Users/j/.agentjobs/DISPATCH_DISABLED",
};

const dispatchRun: DispatchRunView = {
  run_id: "run_abc123",
  task_id: "task-detail",
  project_id: "inbox",
  mode: "session",
  posture: "supervised",
  status: "running",
  outcome: null,
  session_id: null,
  started_at: "2026-08-18T10:00:00Z",
  elapsed_seconds: 42,
  live: true,
  caused_by: 7,
  output_url: "/api/projects/inbox/dispatch/runs/run_abc123/output",
};

function renderPanel(detail: TaskDetailResponse, extra: Partial<TaskDetailProps> = {}) {
  const actions = {
    onApprove: vi.fn(async () => undefined),
    onSendBack: vi.fn(async () => undefined),
    onReject: vi.fn(async () => undefined),
    onPromote: vi.fn(async () => undefined),
    onResume: vi.fn(async () => undefined),
    onAddNote: vi.fn(async () => undefined),
    onSaveFields: vi.fn(async () => undefined),
  };
  const { container } = render(
    <MemoryRouter>
      <TaskDetail detail={detail} projectId="inbox" {...actions} {...extra} />
    </MemoryRouter>,
  );
  return inventory(container);
}

/**
 * The inventory each state offered on `f253223`, the commit this task branched from.
 *
 * Written down rather than snapshotted, and that is the whole point: a snapshot is one
 * `-u` away from recording whatever the panel does today, and what task-239 has to prove
 * is that these lists did not move while the panel was refitted. If a change here is
 * intended, edit the constant in the same commit that intends it and say so in the
 * message. Taken 2026-09-06 by rendering the panel and reading the DOM; the run that
 * produced them is on task-239's record.
 *
 * task-230 adds one entry to every list -- the Fields editor's opener, which renders in
 * every state because editing a task's authoring fields is not gated on who holds the
 * ball. That is the change, and this is the commit intending it. The counts are now
 * 23 / 21 / 21 / 16 / 16, counted from the arrays below rather than quoted: the line
 * they replace claimed 23 / 20 / 20 / 15 / 16 while the arrays held 22 and 15, so two
 * of the five had already drifted from the thing they describe.
 *
 * Duplicated entries are real. `task-child` appears twice because the record both
 * contains that child and is blocked by it, and the two links are in different sections.
 */
const REVIEW = [
  "a: Child titletask-child",
  "a: Parent title (task-parent)",
  "a: The spec",
  "a: task-child",
  "a: task-child",
  "a: task-needed",
  "a: task-noticed-on",
  "a: ← Back to Tasks",
  "button: Expand all entries",
  "button: ↪ New Instructions",
  "button: ⏸ Hold",
  "button: ✎ Add a note",
  "button: ✎ Edit fields",
  "button: ✎ Request Changes",
  "button: ✓ Approve — agent may merge",
  "button: ✓ Send answers",
  "button: ✕ Reject & Archive",
  "input: 🖼Attach a screenshot",
  "summary: Entry",
  "summary: Entry",
  "summary: Entry",
  "textarea: Anything else (optional)",
  "textarea: Your answer",
];

const DRAFT = [
  "a: Child titletask-child",
  "a: Parent title (task-parent)",
  "a: The spec",
  "a: task-child",
  "a: task-child",
  "a: task-needed",
  "a: task-noticed-on",
  "a: ← Back to Tasks",
  "button: Expand all entries",
  "button: ▲ Promote — make it claimable",
  "button: ✎ Add a note",
  "button: ✎ Edit fields",
  "button: ✎ Send feedback",
  "button: ✓ Send answers",
  "button: ✕ Reject & Archive",
  "input: 🖼Attach a screenshot",
  "summary: Entry",
  "summary: Entry",
  "summary: Entry",
  "textarea: Anything else (optional)",
  "textarea: Your answer",
];

const DECIDING = [
  "a: Child titletask-child",
  "a: Parent title (task-parent)",
  "a: The spec",
  "a: task-child",
  "a: task-child",
  "a: task-needed",
  "a: task-noticed-on",
  "a: ← Back to Tasks",
  "button: Expand all entries",
  "button: The first",
  "button: The second",
  "button: ↪ New Instructions",
  "button: ⏸ Hold",
  "button: ✎ Add a note",
  "button: ✎ Edit fields",
  "button: ✓ Send answers",
  "button: ✕ Reject & Archive",
  "input: 🖼Attach a screenshot",
  "summary: Entry",
  "textarea: Anything else (optional)",
  "textarea: Something else",
];

const HELD = [
  "a: Child titletask-child",
  "a: Parent title (task-parent)",
  "a: The spec",
  "a: task-child",
  "a: task-child",
  "a: task-needed",
  "a: task-noticed-on",
  "a: ← Back to Tasks",
  "button: Expand all entries",
  "button: ▶ Resume — release the hold",
  "button: ✎ Add a note",
  "button: ✎ Edit fields",
  "button: ✕ Reject & Archive",
  "summary: Entry",
  "summary: Entry",
  "summary: Entry",
];

const DISPATCHED = [
  "a: Child titletask-child",
  "a: Parent title (task-parent)",
  "a: The spec",
  "a: View output",
  "a: task-child",
  "a: task-child",
  "a: task-needed",
  "a: task-noticed-on",
  "a: ← Back to Tasks",
  "button: Cancel run",
  "button: Expand all entries",
  "button: ✎ Add a note",
  "button: ✎ Edit fields",
  "summary: Entry",
  "summary: Entry",
  "summary: Entry",
];

describe("every action the detail panel offers", () => {
  it("is unchanged on a task waiting for review", () => {
    expect(renderPanel(detailFor())).toEqual(REVIEW);
  });

  it("is unchanged on a draft", () => {
    expect(
      renderPanel(detailFor({ lifecycle: "draft", ball_reason: "spec", display_status: "Draft" })),
    ).toEqual(DRAFT);
  });

  it("is unchanged on a task holding an open question", () => {
    expect(
      renderPanel(
        detailFor({
          ball_reason: "decision",
          display_status: "Waiting for a decision",
          log: [
            {
              id: 1,
              ts: "2026-08-13T08:00:00Z",
              actor: "codex",
              type: "question",
              body: "Which of these two?",
              data: { options: [{ id: "a", label: "The first" }, { id: "b", label: "The second" }] },
            },
          ],
        }),
      ),
    ).toEqual(DECIDING);
  });

  it("is unchanged on a held task", () => {
    expect(
      renderPanel(
        detailFor({ ball: "agent", ball_reason: "hold", display_status: "On hold", ball_prompt: "Held." }),
      ),
    ).toEqual(HELD);
  });

  it("is unchanged on a task an agent is working, with a run live", () => {
    expect(
      renderPanel(
        detailFor({ ball: "agent", ball_reason: "work", display_status: "In progress", ball_prompt: "Carry on." }),
        {
          dispatch: {
            state: dispatchState,
            runs: [dispatchRun],
            onDispatch: vi.fn(async () => true),
            onCancel: vi.fn(async () => undefined),
          },
        },
      ),
    ).toEqual(DISPATCHED);
  });
});
