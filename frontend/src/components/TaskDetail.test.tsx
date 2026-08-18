import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import type { AttachmentUpload, TaskDetailResponse, TaskRead } from "../api/types";
import { TaskDetail } from "./TaskDetail";

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
    dependencies: [{ task: "task-needed", type: "needs", note: "Required first." }],
    log: [
      { id: 1, ts: "2026-08-13T08:00:00Z", actor: "codex", type: "question", body: "Still unanswered?" },
      { id: 2, ts: "2026-08-13T08:30:00Z", actor: "codex", type: "decision", body: "Use the typed detail contract." },
      { id: 3, ts: "2026-08-13T09:00:00Z", actor: "codex", type: "handoff", body: "Ready for review." },
    ],
    ...overrides,
  };
}

const detail: TaskDetailResponse = {
  task: task("task-detail", { parent: "task-parent" }),
  parent_task: task("task-parent", { ball: "agent", ball_reason: "work", ball_prompt: "Continue.", title: "Parent title" }),
  children: [task("task-child", { ball: "agent", ball_reason: "available", ball_prompt: null, lifecycle: "ready", title: "Child title", display_status: "Ready" })],
  needs: [{ task_id: "task-needed", title: "Required task", exists: true, state: "open", note: "Required first.", reason: "Needs task-needed; it is still open." }],
  blocks: [{ task_id: "task-child", title: "Child title", exists: true, state: "open", note: null, reason: "task-child needs this task." }],
  related: [{ task_id: "task-noticed-on", title: "The page it was noticed on", exists: true, state: "open", note: "Reported while viewing this task.", reason: "Related to task-noticed-on." }],
  child_dependency_edges: [{ source: "task-missing", target: "task-child", note: "External gate.", source_exists: false, target_exists: true, source_contained: false, target_contained: true }],
  identity: { ok: true, user: "Jeff Posey", problem: null, detail: "" },
};

function renderDetail(value = detail, extra: { promoteError?: string | null; promoteBusy?: boolean } = {}) {
  const actions = {
    onApprove: vi.fn(async () => undefined),
    // Typed with its parameters so a test can read back what the panel sent, not just
    // that it was called.
    onRequestChanges: vi.fn(
      async (_feedback: string, _attachments: Array<AttachmentUpload>) => undefined,
    ),
    onReject: vi.fn(async () => undefined),
    onPromote: vi.fn(async () => undefined),
  };
  render(<MemoryRouter><TaskDetail detail={value} projectId="inbox" {...actions} {...extra} /></MemoryRouter>);
  return actions;
}

/** A draft as the API returns one: lifecycle draft, ball with the human who wrote it. */
const draftDetail: TaskDetailResponse = {
  ...detail,
  task: task("task-draft", { lifecycle: "draft", ball: "human", ball_reason: "spec", ball_prompt: "Finish the spec.", display_status: "Needs spec" }),
  parent_task: null,
  children: [],
  needs: [],
  blocks: [],
  child_dependency_edges: [],
};

describe("TaskDetail resumption contract", () => {
  it("renders the complete spec and relationships", () => {
    renderDetail();

    const spec = screen.getByRole("region", { name: "Full specification" });
    for (const text of ["Complete summary text.", "Complete intent text.", "Complete working description text.", "Complete constraints text.", "Complete out-of-scope text.", "src/example.py", "Complete context reason.", "Complete acceptance text."]) {
      expect(within(spec).getByText(text, { exact: false })).toBeVisible();
    }
    expect(screen.getByRole("link", { name: /Parent title/ })).toHaveAttribute("href", "/p/inbox/tasks/task-parent");
    expect(screen.getByRole("link", { name: /Child title/ })).toHaveAttribute("href", "/p/inbox/tasks/task-child");
    expect(screen.getByRole("link", { name: "task-needed" })).toHaveAttribute("href", "/p/inbox/tasks/task-needed");
    expect(screen.getByText("Needs task-needed; it is still open.")).toBeVisible();
    expect(screen.getByText("task-child needs this task.")).toBeVisible();
    // A `related` edge blocks nothing, so it never appears in the work state. It is
    // still the trail a reported issue leaves back to the page it was noticed on, so
    // it has to be followable.
    expect(screen.getByRole("link", { name: "task-noticed-on" })).toHaveAttribute("href", "/p/inbox/tasks/task-noticed-on");
    expect(screen.getByText("Reported while viewing this task.")).toBeVisible();
    expect(screen.getByRole("region", { name: "Umbrella dependency graph" })).toHaveTextContent("task-missing (missing)");
    expect(screen.getByRole("region", { name: "Dependency state" })).toHaveTextContent("Waiting for review");
  });

  it("surfaces dependency cycles as data errors without hiding graph nodes", () => {
    const cycle = ["task-child", "task-other", "task-child"];
    renderDetail({
      ...detail,
      children: [
        task("task-child", { title: "Child title", needs_cycles: [cycle] }),
        task("task-other", { title: "Other child", needs_cycles: [cycle] }),
      ],
      child_dependency_edges: [
        { source: "task-child", target: "task-other", note: null, source_exists: true, target_exists: true, source_contained: true, target_contained: true },
        { source: "task-other", target: "task-child", note: null, source_exists: true, target_exists: true, source_contained: true, target_contained: true },
      ],
    });

    const graph = screen.getByRole("region", { name: "Umbrella dependency graph" });
    expect(within(graph).getByRole("alert")).toHaveTextContent("Dependency data error");
    expect(within(graph).getByText("Child title")).toBeVisible();
    expect(within(graph).getByText("Other child")).toBeVisible();
  });

  it("renders the log newest-first and distinguishes decisions and open questions", () => {
    renderDetail();

    const entries = within(screen.getByRole("region", { name: "Task log" })).getAllByRole("article");
    expect(entries.map((entry) => entry.getAttribute("data-log-id"))).toEqual(["3", "2", "1"]);
    expect(screen.getByText("decision", { exact: true })).toBeVisible();
    expect(screen.getByText("open question", { exact: true })).toBeVisible();
  });

  it("submits configured-identity approval and change requests", async () => {
    const actions = renderDetail();

    fireEvent.click(screen.getByRole("button", { name: /Approve/ }));
    await waitFor(() => expect(actions.onApprove).toHaveBeenCalledOnce());

    fireEvent.click(screen.getByRole("button", { name: /Request Changes/ }));
    fireEvent.change(screen.getByLabelText("Feedback or questions"), { target: { value: "Tighten the layout." } });
    fireEvent.click(screen.getByRole("button", { name: "Submit" }));
    await waitFor(() => expect(actions.onRequestChanges).toHaveBeenCalledWith("Tighten the layout.", []));
  });

  it("blocks every review action when identity is unclear", () => {
    renderDetail({ ...detail, identity: { ok: false, user: null, problem: "multiple", detail: "Cannot choose safely." } });

    expect(screen.getByText("Cannot choose safely.")).toBeVisible();
    expect(screen.queryByRole("button", { name: /Approve/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Request Changes/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Reject/ })).not.toBeInTheDocument();
  });
});

describe("TaskDetail action panel speaks the phase it is in", () => {
  it("offers planning actions on a draft, in one panel", () => {
    renderDetail(draftDetail);

    const panel = screen.getByRole("region", { name: "Draft actions" });
    expect(within(panel).getByRole("button", { name: "▲ Promote — make it claimable" })).toBeVisible();
    expect(within(panel).getByRole("button", { name: "✎ Send feedback" })).toBeVisible();
    expect(within(panel).getByRole("button", { name: "✕ Reject & Archive" })).toBeVisible();

    // The merge-review vocabulary must not appear on a task whose work has not
    // started -- there is nothing to approve and nothing to merge yet.
    expect(within(panel).queryByRole("button", { name: /Approve/ })).not.toBeInTheDocument();
    expect(within(panel).queryByText(/merge/i)).not.toBeInTheDocument();
    // One panel, not two.
    expect(screen.getAllByRole("region", { name: /actions$/ })).toHaveLength(1);
  });

  it("keeps the review actions on a task past draft", () => {
    renderDetail();

    const panel = screen.getByRole("region", { name: "Review actions" });
    expect(within(panel).getByRole("button", { name: "✓ Approve — agent may merge" })).toBeVisible();
    expect(within(panel).getByRole("button", { name: "✎ Request Changes" })).toBeVisible();
    expect(within(panel).getByRole("button", { name: "✕ Reject & Archive" })).toBeVisible();
    expect(within(panel).queryByRole("button", { name: /Promote/ })).not.toBeInTheDocument();
    expect(screen.getAllByRole("region", { name: /actions$/ })).toHaveLength(1);
  });

  it("sends a typed note, and null when the note is left empty", async () => {
    const withNote = renderDetail(draftDetail);
    fireEvent.click(screen.getByRole("button", { name: /Promote — make it claimable/ }));
    fireEvent.change(screen.getByLabelText("Promotion note (optional)"), { target: { value: "  Spec is finished.  " } });
    fireEvent.click(screen.getByRole("button", { name: "Promote" }));
    // Trimmed, so trailing whitespace does not become the log body.
    await waitFor(() => expect(withNote.onPromote).toHaveBeenCalledWith("Spec is finished."));

    cleanup();

    const withoutNote = renderDetail(draftDetail);
    fireEvent.click(screen.getByRole("button", { name: /Promote — make it claimable/ }));
    fireEvent.click(screen.getByRole("button", { name: "Promote" }));
    // null, not "", so the manager supplies its own default body.
    await waitFor(() => expect(withoutNote.onPromote).toHaveBeenCalledWith(null));
  });

  it("never sends a draft through approve", async () => {
    // approve leaves lifecycle alone, so a draft sent through it would sit at
    // draft/agent-work, which get_next_task() never returns.
    const actions = renderDetail(draftDetail);
    fireEvent.click(screen.getByRole("button", { name: /Promote — make it claimable/ }));
    fireEvent.click(screen.getByRole("button", { name: "Promote" }));
    await waitFor(() => expect(actions.onPromote).toHaveBeenCalled());
    expect(actions.onApprove).not.toHaveBeenCalled();
  });

  it("sends a pasted screenshot along with the feedback it evidences", async () => {
    const actions = renderDetail();

    fireEvent.click(screen.getByRole("button", { name: /Request Changes/ }));
    const box = screen.getByRole("textbox", { name: "Feedback or questions" });
    fireEvent.change(box, { target: { value: "The badge shows the enum name." } });
    // No file picker in this path: a screenshot arrives as a blob on the paste event.
    fireEvent.paste(box, {
      clipboardData: {
        items: [
          {
            kind: "file",
            getAsFile: () => new File([new Uint8Array(8)], "badge.png", { type: "image/png" }),
          },
        ],
        files: [],
      },
    });

    await screen.findByRole("list", { name: "Attached images" });
    fireEvent.click(screen.getByRole("button", { name: "Submit" }));
    await waitFor(() => expect(actions.onRequestChanges).toHaveBeenCalled());
    const call = actions.onRequestChanges.mock.calls[0]!;
    expect(call[0]).toBe("The badge shows the enum name.");
    expect(call[1]).toHaveLength(1);
    expect(call[1][0]?.label).toBe("badge.png");
  });

  it("shows an entry's images where the entry is read, not as bare links", () => {
    renderDetail({
      ...detail,
      task: task("task-detail", {
        parent: "task-parent",
        log: [
          {
            id: 1,
            ts: "2026-08-17T09:00:00Z",
            actor: "Jeff Posey",
            type: "handoff",
            body: "Changes requested: the badge shows the enum name.",
            attachments: [
              {
                path: "attachments/task-detail/abc123.png",
                media_type: "image/png",
                sha256: "abc123",
                size_bytes: 2048,
                label: "The badge",
              },
            ],
          },
        ],
      }),
    });

    const image = screen.getByRole("img", { name: "The badge" });
    expect(image).toHaveAttribute(
      "src",
      "/api/projects/inbox/tasks/task-detail/attachments/abc123.png",
    );
  });

  it("still sends feedback and rejections through the unchanged actions on a draft", async () => {
    const actions = renderDetail(draftDetail);

    fireEvent.click(screen.getByRole("button", { name: /Send feedback/ }));
    fireEvent.change(screen.getByLabelText("Feedback on the spec"), { target: { value: "Acceptance is vague." } });
    fireEvent.click(screen.getByRole("button", { name: "Submit" }));
    await waitFor(() => expect(actions.onRequestChanges).toHaveBeenCalledWith("Acceptance is vague.", []));

    fireEvent.click(screen.getByRole("button", { name: /Reject & Archive/ }));
    fireEvent.change(screen.getByLabelText("Reason for rejection"), { target: { value: "Superseded." } });
    fireEvent.click(screen.getByRole("button", { name: "Submit" }));
    await waitFor(() => expect(actions.onReject).toHaveBeenCalledWith("Superseded."));
  });

  it("shows a promote refusal rather than swallowing it", () => {
    renderDetail(draftDetail, { promoteError: "Task 'task-draft' is not a draft (it is ready); only a draft can be promoted." });
    expect(screen.getByRole("alert")).toHaveTextContent("only a draft can be promoted");
  });

  it("still shows the refusal once the ball has left the human", () => {
    // The revision-conflict case: someone else promoted it, which both caused the
    // refusal and moved the ball to the agent -- so the panel itself is gone. Inside
    // the panel, this message would vanish exactly when it is needed.
    renderDetail(
      { ...draftDetail, task: task("task-draft", { lifecycle: "ready", ball: "agent", ball_reason: "available" }) },
      { promoteError: "This task changed while the page was open, so it was not promoted." },
    );
    expect(screen.queryByRole("region", { name: /actions$/ })).not.toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("changed while the page was open");
  });

  it("disables the actions while a promotion is in flight", () => {
    renderDetail(draftDetail, { promoteBusy: true });
    expect(screen.getByRole("button", { name: /Promote — make it claimable/ })).toBeDisabled();
    expect(screen.getByRole("button", { name: /Send feedback/ })).toBeDisabled();
  });

  it("offers no action at all when identity is unclear", () => {
    renderDetail({ ...draftDetail, identity: { ok: false, user: null, problem: "missing", detail: "No user configured in this project." } });

    const panel = screen.getByRole("region", { name: "Draft actions" });
    expect(within(panel).getByText("No user configured in this project.")).toBeVisible();
    expect(within(panel).queryByRole("button", { name: /Promote/ })).not.toBeInTheDocument();
    expect(within(panel).queryByRole("button", { name: /Send feedback/ })).not.toBeInTheDocument();
  });
});
