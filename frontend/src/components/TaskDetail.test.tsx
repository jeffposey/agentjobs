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
    onApprove: vi.fn(async (_note: string | null) => undefined),
    // Typed with its parameters so a test can read back what the panel sent, not just
    // that it was called. `reason` is first because it is the thing worth asserting:
    // a control whose label says "Answer Questions" and whose call says "revise" is
    // exactly the defect this contract exists to make visible.
    onSendBack: vi.fn(
      async (
        _reason: "revise" | "answer" | "redirect" | "hold",
        _feedback: string,
        _attachments: Array<AttachmentUpload>,
      ) => undefined,
    ),
    onReject: vi.fn(async () => undefined),
    onPromote: vi.fn(async () => undefined),
    onResume: vi.fn(async (_note: string | null) => undefined),
    onAddNote: vi.fn(async (_body: string) => undefined),
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

  it("opens every collapsed log body with one control, and closes them again", () => {
    // A long entry is collapsed by default, which keeps a hundred-entry log scannable
    // but hides its text from browser find and from select-all-and-copy. Auditing a
    // long task is exactly when a reviewer needs all of it at once.
    const long = "x".repeat(500);
    renderDetail({
      ...detail,
      task: task("task-long-log", {
        log: [
          { id: 1, ts: "2026-08-13T08:00:00Z", actor: "codex", type: "progress", body: `first ${long}` },
          { id: 2, ts: "2026-08-13T08:30:00Z", actor: "codex", type: "progress", body: `second ${long}` },
          { id: 3, ts: "2026-08-13T09:00:00Z", actor: "codex", type: "handoff", body: "Ready for review." },
        ],
      }),
    });

    const log = screen.getByRole("region", { name: "Task log" });
    const bodies = () => Array.from(log.querySelectorAll("details"));
    // Entry 3 is newest and stays open; entries 2 and 1 are long and start collapsed.
    expect(bodies().map((node) => node.open)).toEqual([true, false, false]);

    const toggle = screen.getByRole("button", { name: "Expand all entries" });
    fireEvent.click(toggle);
    expect(bodies().every((node) => node.open)).toBe(true);
    expect(screen.getByRole("button", { name: "Collapse long entries" })).toHaveAttribute("aria-pressed", "true");

    fireEvent.click(screen.getByRole("button", { name: "Collapse long entries" }));
    expect(bodies().map((node) => node.open)).toEqual([true, false, false]);
  });

  it("re-opens an entry the reader had closed by hand when expand-all is pressed", () => {
    const long = "y".repeat(500);
    renderDetail({
      ...detail,
      task: task("task-hand-toggled", {
        log: [
          { id: 1, ts: "2026-08-13T08:00:00Z", actor: "codex", type: "progress", body: `only ${long}` },
        ],
      }),
    });

    const log = screen.getByRole("region", { name: "Task log" });
    const entry = () => log.querySelector("details") as HTMLDetailsElement;
    // Newest entry, so it starts open; the reader collapses it themselves.
    entry().open = false;

    fireEvent.click(screen.getByRole("button", { name: "Expand all entries" }));

    expect(entry().open).toBe(true);
  });

  it("submits configured-identity approval and change requests", async () => {
    const actions = renderDetail();

    fireEvent.click(screen.getByRole("button", { name: /Approve/ }));
    fireEvent.click(screen.getByRole("button", { name: "Approve" }));
    // null, not "": an approval with nothing attached must write exactly the record it
    // wrote before the note existed.
    await waitFor(() => expect(actions.onApprove).toHaveBeenCalledWith(null));

    fireEvent.click(screen.getByRole("button", { name: /Request Changes/ }));
    fireEvent.change(screen.getByLabelText("Feedback or questions"), { target: { value: "Tighten the layout." } });
    fireEvent.click(screen.getByRole("button", { name: "Submit" }));
    await waitFor(() => expect(actions.onSendBack).toHaveBeenCalledWith("revise", "Tighten the layout.", []));
  });

  it("blocks every review action when identity is unclear", () => {
    renderDetail({ ...detail, identity: { ok: false, user: null, problem: "multiple", detail: "Cannot choose safely." } });

    // Scoped to the review panel: the note composer states the same identity problem
    // for the same reason, so an unscoped query now matches twice.
    expect(
      within(screen.getByRole("region", { name: "Review actions" })).getByText("Cannot choose safely."),
    ).toBeVisible();
    expect(screen.queryByRole("button", { name: /Approve/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Request Changes/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Hold/ })).not.toBeInTheDocument();
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
    // A draft has nothing running, so there is nothing to re-brief and nothing to
    // stop. Both controls appear only once work is underway.
    expect(within(panel).queryByRole("button", { name: /New Instructions/ })).not.toBeInTheDocument();
    expect(within(panel).queryByRole("button", { name: /Hold/ })).not.toBeInTheDocument();
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
    await waitFor(() => expect(actions.onSendBack).toHaveBeenCalled());
    const call = actions.onSendBack.mock.calls[0]!;
    expect(call[0]).toBe("revise");
    expect(call[1]).toBe("The badge shows the enum name.");
    expect(call[2]).toHaveLength(1);
    expect(call[2][0]?.label).toBe("badge.png");
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
    await waitFor(() => expect(actions.onSendBack).toHaveBeenCalledWith("revise", "Acceptance is vague.", []));

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

/**
 * Task-185: a `ready` task whose ball is with an agent shows only Dispatch, and a
 * dispatch it refuses tells the reader to write an authorising entry. The control that
 * writes one has to be on this page, on this state, or the refusal is a dead end again.
 */
describe("TaskDetail offers a way to write on the record", () => {
  const readyDetail: TaskDetailResponse = {
    ...detail,
    task: task("task-ready", {
      lifecycle: "ready",
      ball: "agent",
      ball_reason: "available",
      ball_prompt: null,
      display_status: "Ready",
      assignment: { owner: null, eligible: [] },
      log: [
        { id: 1, ts: "2026-08-19T21:16:19Z", actor: "claude", type: "transition", body: "Created ready by claude." },
      ],
    }),
    parent_task: null,
    children: [],
    needs: [],
    blocks: [],
    child_dependency_edges: [],
  };

  it("puts the note control on a ready task, where no review action is offered", () => {
    renderDetail(readyDetail);

    expect(screen.queryByRole("region", { name: /actions$/ })).not.toBeInTheDocument();
    const notes = screen.getByRole("region", { name: "Notes" });
    expect(within(notes).getByRole("button", { name: /add a note/i })).toBeVisible();
  });

  it("sends the note the reader typed", async () => {
    const actions = renderDetail(readyDetail);

    fireEvent.click(screen.getByRole("button", { name: /add a note/i }));
    fireEvent.change(screen.getByLabelText("Note"), { target: { value: "Go ahead." } });
    fireEvent.click(screen.getByRole("button", { name: "Save note" }));

    await waitFor(() => expect(actions.onAddNote).toHaveBeenCalledWith("Go ahead."));
  });
});

/**
 * task-231 part 2: the panel offers verbs that are true of the task in front of it.
 *
 * Every assertion here reads the rendered button text and the argument the control
 * actually sends, never the presence of markup. That is ENGINEERING.md's rule, and it
 * is the rule this component has already broken once: `data-ball="Ball.HUMAN"` passed
 * every check that asked whether an attribute existed.
 */
describe("TaskDetail review panel offers only verbs that are true", () => {
  function atReason(ball_reason: string, extra: Partial<TaskRead> = {}) {
    return {
      ...detail,
      task: task("task-reasoned", {
        ball: "human",
        ball_reason,
        display_status: "Needs decision",
        ball_prompt: "Four numbered questions.",
        ...extra,
      } as Partial<TaskRead>),
    };
  }

  it.each(["decision", "input"])(
    "offers no Approve on a task waiting on %s, and asks for an answer instead",
    (reason) => {
      renderDetail(atReason(reason));

      const panel = screen.getByRole("region", { name: "Review actions" });
      // The defect verbatim: on task-077 this button read "✓ Approve — agent may
      // merge" over four numbered questions, and there was no branch to merge.
      expect(within(panel).queryByRole("button", { name: /Approve/ })).not.toBeInTheDocument();
      expect(within(panel).queryByText(/merge/i)).not.toBeInTheDocument();
      expect(within(panel).getByRole("button", { name: "✎ Answer Questions" })).toBeVisible();
      expect(within(panel).getByRole("button", { name: "✕ Reject & Archive" })).toBeVisible();
    },
  );

  it("records an answer as an answer, not as a revision", async () => {
    const actions = renderDetail(atReason("decision"));

    fireEvent.click(screen.getByRole("button", { name: "✎ Answer Questions" }));
    fireEvent.change(screen.getByLabelText("Your answer"), { target: { value: "Option 2, and skip the third." } });
    fireEvent.click(screen.getByRole("button", { name: "Submit" }));

    await waitFor(() =>
      expect(actions.onSendBack).toHaveBeenCalledWith("answer", "Option 2, and skip the third.", []),
    );
  });

  it("keeps Approve on a task at approval, where approving means something", () => {
    renderDetail(atReason("approval", { display_status: "Needs approval" }));

    const panel = screen.getByRole("region", { name: "Review actions" });
    expect(within(panel).getByRole("button", { name: "✓ Approve — agent may merge" })).toBeVisible();
    expect(within(panel).getByRole("button", { name: "✎ Request Changes" })).toBeVisible();
  });

  it("offers no merge verb on a spec that is past draft", () => {
    renderDetail(atReason("spec", { lifecycle: "active", display_status: "Needs spec" }));

    const panel = screen.getByRole("region", { name: "Review actions" });
    expect(within(panel).queryByRole("button", { name: /Approve/ })).not.toBeInTheDocument();
    expect(within(panel).queryByText(/merge/i)).not.toBeInTheDocument();
    // Not Promote either: the task is not a draft, so there is nothing to promote.
    expect(within(panel).queryByRole("button", { name: /Promote/ })).not.toBeInTheDocument();
    expect(within(panel).getByRole("button", { name: "✎ Send feedback" })).toBeVisible();
  });

  it("records a re-brief as a redirect, so a reader can tell it from a rejection", async () => {
    const actions = renderDetail();

    fireEvent.click(screen.getByRole("button", { name: "↪ New Instructions" }));
    fireEvent.change(screen.getByLabelText("New instructions"), { target: { value: "Do the CLI half first." } });
    fireEvent.click(screen.getByRole("button", { name: "Submit" }));

    await waitFor(() =>
      expect(actions.onSendBack).toHaveBeenCalledWith("redirect", "Do the CLI half first.", []),
    );
  });

  it("records a hold as a hold, with the release condition as its payload", async () => {
    const actions = renderDetail();

    fireEvent.click(screen.getByRole("button", { name: "⏸ Hold" }));
    fireEvent.change(screen.getByLabelText("Release condition"), { target: { value: "Wait for the dispatch fixes." } });
    fireEvent.click(screen.getByRole("button", { name: "Submit" }));

    await waitFor(() =>
      expect(actions.onSendBack).toHaveBeenCalledWith("hold", "Wait for the dispatch fixes.", []),
    );
  });

  it("gives a held task a way out, and offers it none of the review verbs", async () => {
    const actions = renderDetail({
      ...detail,
      task: task("task-held", {
        ball: "agent",
        ball_reason: "hold",
        ball_prompt: "ON HOLD -- do not resume until the dispatch fixes land.",
        display_status: "On hold (codex)",
      }),
    });

    // The ball is with the agent, so before this the panel returned null and a hold
    // imposed from the browser could not be released from the browser.
    const panel = screen.getByRole("region", { name: "Hold actions" });
    expect(within(panel).getByText("ON HOLD -- do not resume until the dispatch fixes land.")).toBeVisible();
    expect(within(panel).queryByRole("button", { name: /Approve/ })).not.toBeInTheDocument();
    expect(within(panel).queryByRole("button", { name: /Request Changes/ })).not.toBeInTheDocument();

    fireEvent.click(within(panel).getByRole("button", { name: "▶ Resume — release the hold" }));
    fireEvent.click(screen.getByRole("button", { name: "Resume" }));
    await waitFor(() => expect(actions.onResume).toHaveBeenCalledWith(null));
  });

  it("closes the composer once a hold lands, since the panel does not go away", async () => {
    // Every other send-back moves the ball off the human and takes the panel with it.
    // A hold does not -- it becomes the held panel -- so the form that imposed the
    // hold was left open underneath, still offering Submit. Found in a browser.
    const actions = renderDetail();

    fireEvent.click(screen.getByRole("button", { name: "⏸ Hold" }));
    fireEvent.change(screen.getByLabelText("Release condition"), { target: { value: "Wait for the invoice." } });
    fireEvent.click(screen.getByRole("button", { name: "Submit" }));

    await waitFor(() => expect(actions.onSendBack).toHaveBeenCalled());
    await waitFor(() => expect(screen.queryByRole("button", { name: "Submit" })).not.toBeInTheDocument());
    expect(screen.queryByLabelText("Release condition")).not.toBeInTheDocument();
  });

  it("keeps what the human typed when a send-back fails", async () => {
    // The banner above the form reports the failure. Throwing the prose away as well
    // would make the human retype it to find out whether the second attempt works.
    const actions = renderDetail();
    actions.onSendBack.mockRejectedValueOnce(new Error("the network, briefly"));

    fireEvent.click(screen.getByRole("button", { name: "⏸ Hold" }));
    fireEvent.change(screen.getByLabelText("Release condition"), { target: { value: "Wait for the invoice." } });
    fireEvent.click(screen.getByRole("button", { name: "Submit" }));

    await waitFor(() => expect(actions.onSendBack).toHaveBeenCalled());
    expect(screen.getByLabelText("Release condition")).toHaveValue("Wait for the invoice.");
  });

  it("carries an approval note without turning the approval into a request for changes", async () => {
    const actions = renderDetail();

    fireEvent.click(screen.getByRole("button", { name: "✓ Approve — agent may merge" }));
    fireEvent.change(screen.getByLabelText("Approval note (optional) — does not block the merge"), {
      target: { value: "  Fold the naming nit in first.  " },
    });
    fireEvent.click(screen.getByRole("button", { name: "Approve" }));

    // Trimmed, and it goes through approve. Before this it had to go through Request
    // Changes, which recorded approved work as `revise` and asked for a round trip
    // nobody wanted.
    await waitFor(() => expect(actions.onApprove).toHaveBeenCalledWith("Fold the naming nit in first."));
    expect(actions.onSendBack).not.toHaveBeenCalled();
  });
});
