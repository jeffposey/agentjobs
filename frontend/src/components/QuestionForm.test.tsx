import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import type { AnswerSubmission } from "../api/generated";
import type { AttachmentUpload, TaskDetailResponse, TaskRead } from "../api/types";
import { TaskDetail } from "./TaskDetail";

/**
 * Answering structured questions from the panel (task-017).
 *
 * These drive the real panel rather than the form in isolation, because the thing worth
 * proving is the round trip a person makes: open the composer, tap, submit, and have the
 * call carry the right answers threaded to the right questions. Asserting on rendered
 * option text and on what `onSendBack` received, per the task's own instruction not to
 * assert on markup being present.
 */

//: task-077's real handoff, 2026-08-18. Four questions; the third wants a number and Jeff
//: took none of the three options offered, which is why free text is a constraint here.
const QUESTIONS = [
  {
    id: 10,
    ts: "2026-08-18T05:11:00Z",
    actor: "claude",
    type: "question",
    re: 9,
    body: "Session mode, or supervise our own processes?",
    data: {
      options: [
        {
          label: "Session mode primary, batch retained",
          description: "`--bg --remote-control`, with `-p` kept as a declared mode.",
          recommended: true,
        },
        { label: "Session-only, delete batch", description: "One code path.", recommended: false },
        { label: "Batch-only", description: "No mid-flight redirect.", recommended: false },
      ],
      multi_select: false,
      placeholder: null,
    },
  },
  {
    id: 11,
    ts: "2026-08-18T05:11:00Z",
    actor: "claude",
    type: "question",
    re: 9,
    body: "Kill runs when the supervisor restarts?",
    data: {
      options: [
        { label: "No -- re-attach", description: null, recommended: true },
        { label: "Keep the rule", description: null, recommended: false },
      ],
      multi_select: false,
      placeholder: null,
    },
  },
  {
    id: 12,
    ts: "2026-08-18T05:11:00Z",
    actor: "claude",
    type: "question",
    re: 9,
    body: "How long before an idle session counts as stalled?",
    data: {
      options: [
        { label: "15 minutes", description: null, recommended: false },
        { label: "4 hours", description: null, recommended: false },
        { label: "Keep the 1800s hard kill", description: null, recommended: false },
      ],
      multi_select: false,
      placeholder: "a number of minutes",
    },
  },
  {
    id: 13,
    ts: "2026-08-18T05:11:00Z",
    actor: "claude",
    type: "question",
    re: 9,
    body: "What happens to task-070 and task-072?",
    data: {
      options: [
        { label: "Rescope task-070 to batch mode", description: null, recommended: true },
        { label: "Rescope task-072 to our own ledger", description: null, recommended: true },
        { label: "Close task-072 as superseded", description: null, recommended: false },
      ],
      multi_select: true,
      placeholder: null,
    },
  },
];

function task(overrides: Partial<TaskRead> = {}): TaskRead {
  return {
    schema: 2,
    id: "task-077",
    title: "Dispatch session launcher",
    created: "2026-08-18T05:00:00Z",
    updated: "2026-08-18T05:11:00Z",
    lifecycle: "active",
    ball: "human",
    ball_reason: "decision",
    ball_prompt: "sc-1 verified; four decisions needed.",
    display_status: "Needs decision",
    priority: "high",
    category: "infra",
    tags: [],
    assignment: { owner: "claude", eligible: [] },
    spec: { summary: "Summary.", description: "Description." },
    log: [
      { id: 9, ts: "2026-08-18T05:11:00Z", actor: "claude", type: "handoff", body: "Four decisions." },
      ...QUESTIONS,
    ],
    ...overrides,
  } as TaskRead;
}

function detailFor(value: TaskRead): TaskDetailResponse {
  return {
    task: value,
    parent_task: null,
    children: [],
    needs: [],
    blocks: [],
    related: [],
    child_dependency_edges: [],
    identity: { ok: true, user: "Jeff Posey", problem: null, detail: "" },
  };
}

function renderPanel(value: TaskRead = task()) {
  const actions = {
    onApprove: vi.fn(async (_note: string | null) => undefined),
    onSendBack: vi.fn(
      async (
        _reason: "revise" | "answer" | "redirect" | "hold",
        _feedback: string,
        _attachments: Array<AttachmentUpload>,
        _answers: Array<AnswerSubmission>,
      ) => undefined,
    ),
    onReject: vi.fn(async () => undefined),
    onPromote: vi.fn(async () => undefined),
    onResume: vi.fn(async (_note: string | null) => undefined),
    onAddNote: vi.fn(async (_body: string) => undefined),
  };
  render(
    <MemoryRouter>
      <TaskDetail detail={detailFor(value)} projectId="agentjobs" {...actions} />
    </MemoryRouter>,
  );
  return actions;
}

function openComposer() {
  fireEvent.click(screen.getByRole("button", { name: "✎ Answer Questions" }));
}

/** The one call the panel made, so a test can read back what it sent rather than that it sent. */
function sent(actions: ReturnType<typeof renderPanel>) {
  const call = actions.onSendBack.mock.calls[0];
  if (!call) throw new Error("onSendBack was never called.");
  return { reason: call[0], feedback: call[1], answers: call[3] };
}

function answersFrom(actions: ReturnType<typeof renderPanel>): Array<AnswerSubmission> {
  return sent(actions).answers;
}

describe("answering structured questions", () => {
  it("renders every open question with its options and the agent's recommendation", () => {
    renderPanel();
    openComposer();

    for (const question of QUESTIONS) {
      expect(screen.getByText(`${QUESTIONS.indexOf(question) + 1}. ${question.body}`)).toBeVisible();
      for (const option of question.data.options) {
        expect(screen.getByRole("button", { name: new RegExp(escape(option.label)) })).toBeVisible();
      }
    }
    // The recommendation is a mark, never a preselection: nothing is chosen on arrival.
    expect(screen.getAllByText("Recommended")).toHaveLength(4);
    expect(screen.queryAllByRole("button", { pressed: true })).toHaveLength(0);
  });

  it("puts a free-text box on every question, options or not", () => {
    renderPanel();
    openComposer();

    // Four questions, all of which offer options, and all of which can still be
    // answered in words. This is the constraint task-017 is most insistent about.
    expect(screen.getAllByLabelText("Something else")).toHaveLength(4);
    // The one whose answer is a number says so, rather than making the reader guess.
    expect(screen.getByPlaceholderText("a number of minutes")).toBeVisible();
  });

  it("sends the tapped options as answers threaded to their questions", async () => {
    const actions = renderPanel();
    openComposer();

    fireEvent.click(screen.getByRole("button", { name: /Session mode primary/ }));
    fireEvent.click(screen.getByRole("button", { name: /No -- re-attach/ }));
    fireEvent.click(screen.getByRole("button", { name: "Submit" }));

    await waitFor(() => expect(actions.onSendBack).toHaveBeenCalled());
    expect(sent(actions).reason).toBe("answer");
    expect(answersFrom(actions)).toEqual([
      { re: 10, selected: ["Session mode primary, batch retained"], other: null },
      { re: 11, selected: ["No -- re-attach"], other: null },
    ]);
  });

  it("can be submitted with nothing typed at all", async () => {
    const actions = renderPanel();
    openComposer();

    // The whole point: four taps on a phone, no keyboard. Submit is live once one
    // option is chosen, and the prose field stays empty.
    fireEvent.click(screen.getByRole("button", { name: /Batch-only/ }));
    fireEvent.click(screen.getByRole("button", { name: "Submit" }));

    await waitFor(() => expect(actions.onSendBack).toHaveBeenCalled());
    expect(sent(actions).feedback).toBe("");
    expect(answersFrom(actions)).toEqual([{ re: 10, selected: ["Batch-only"], other: null }]);
  });

  it("refuses to submit until something has been said", () => {
    renderPanel();
    openComposer();

    expect(screen.getByRole("button", { name: "Submit" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: /4 hours/ }));
    expect(screen.getByRole("button", { name: "Submit" })).toBeEnabled();
  });

  it("carries free text that rejects every option offered", async () => {
    const actions = renderPanel();
    openComposer();

    // Jeff's actual answer to question 3 on 2026-08-18: none of 15 minutes, 4 hours or
    // the hard kill. A form that could not capture this would be worse than a prose box.
    fireEvent.change(screen.getByPlaceholderText("a number of minutes"), {
      target: { value: "60 minutes" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Submit" }));

    await waitFor(() => expect(actions.onSendBack).toHaveBeenCalled());
    expect(answersFrom(actions)).toEqual([{ re: 12, selected: [], other: "60 minutes" }]);
  });

  it("carries an option and free text together", async () => {
    const actions = renderPanel();
    openComposer();

    fireEvent.click(screen.getByRole("button", { name: /Keep the rule/ }));
    fireEvent.change(within(screen.getByText("2. Kill runs when the supervisor restarts?").closest("fieldset")!).getByLabelText("Something else"), {
      target: { value: "but log it loudly" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Submit" }));

    await waitFor(() => expect(actions.onSendBack).toHaveBeenCalled());
    expect(answersFrom(actions)).toEqual([
      { re: 11, selected: ["Keep the rule"], other: "but log it loudly" },
    ]);
  });

  it("takes several options only where the question said it could", async () => {
    const actions = renderPanel();
    openComposer();

    fireEvent.click(screen.getByRole("button", { name: /Rescope task-070/ }));
    fireEvent.click(screen.getByRole("button", { name: /Rescope task-072/ }));
    // A single-select question replaces rather than accumulates, so the second tap on
    // question 1 leaves one answer, not two.
    fireEvent.click(screen.getByRole("button", { name: /Session-only/ }));
    fireEvent.click(screen.getByRole("button", { name: /Batch-only/ }));
    fireEvent.click(screen.getByRole("button", { name: "Submit" }));

    await waitFor(() => expect(actions.onSendBack).toHaveBeenCalled());
    expect(answersFrom(actions)).toEqual([
      { re: 10, selected: ["Batch-only"], other: null },
      {
        re: 13,
        selected: ["Rescope task-070 to batch mode", "Rescope task-072 to our own ledger"],
        other: null,
      },
    ]);
  });

  it("lets a single-select answer be taken back", () => {
    renderPanel();
    openComposer();

    const option = screen.getByRole("button", { name: /Batch-only/ });
    fireEvent.click(option);
    expect(option).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(option);
    expect(option).toHaveAttribute("aria-pressed", "false");
    expect(screen.getByRole("button", { name: "Submit" })).toBeDisabled();
  });

  it("answers all four of task-077's questions in one submit", async () => {
    const actions = renderPanel();
    openComposer();

    fireEvent.click(screen.getByRole("button", { name: /Session mode primary/ }));
    fireEvent.click(screen.getByRole("button", { name: /No -- re-attach/ }));
    fireEvent.change(screen.getByPlaceholderText("a number of minutes"), {
      target: { value: "60 minutes" },
    });
    fireEvent.click(screen.getByRole("button", { name: /Rescope task-070/ }));
    fireEvent.click(screen.getByRole("button", { name: /Rescope task-072/ }));
    fireEvent.click(screen.getByRole("button", { name: "Submit" }));

    await waitFor(() => expect(actions.onSendBack).toHaveBeenCalledTimes(1));
    expect(answersFrom(actions).map((answer) => answer.re)).toEqual([10, 11, 12, 13]);
  });

  it("hides a question once it has an answer threaded to it", () => {
    renderPanel(
      task({
        log: [
          ...task().log!,
          { id: 14, ts: "2026-08-18T05:33:00Z", actor: "Jeff Posey", type: "answer", re: 12, body: "60 minutes" },
        ],
      }),
    );
    openComposer();

    expect(screen.queryByPlaceholderText("a number of minutes")).not.toBeInTheDocument();
    expect(screen.getAllByLabelText("Something else")).toHaveLength(3);
  });

  it("falls back to prose when the task has no open questions", () => {
    renderPanel(task({ log: [{ id: 9, ts: "2026-08-18T05:11:00Z", actor: "claude", type: "handoff", body: "Decide." }] }));
    openComposer();

    expect(screen.queryByText("Something else")).not.toBeInTheDocument();
    // The prose box keeps its own label and stays required, exactly as before task-017.
    expect(screen.getByLabelText("Your answer")).toBeRequired();
  });

  it("offers answering on a task waiting on review that still has a question open", () => {
    renderPanel(task({ ball_reason: "review", display_status: "Waiting for review" }));

    const panel = screen.getByRole("region", { name: "Review actions" });
    // Approve stays first, because reviewing is still what the task is for.
    expect(within(panel).getByRole("button", { name: /Approve/ })).toBeVisible();
    expect(within(panel).getByRole("button", { name: "✎ Answer Questions" })).toBeVisible();
  });

  it("shows no answering verb on a review task with nothing open", () => {
    renderPanel(
      task({
        ball_reason: "review",
        display_status: "Waiting for review",
        log: [{ id: 9, ts: "2026-08-18T05:11:00Z", actor: "claude", type: "handoff", body: "Review it." }],
      }),
    );

    const panel = screen.getByRole("region", { name: "Review actions" });
    expect(within(panel).queryByRole("button", { name: "✎ Answer Questions" })).not.toBeInTheDocument();
  });

  it("renders a question written before options existed as a plain one", () => {
    renderPanel(
      task({
        log: [
          { id: 9, ts: "2026-08-18T05:11:00Z", actor: "claude", type: "handoff", body: "Decide." },
          // Every question in this repository's corpus predates task-017 and looks
          // like this: no payload at all.
          { id: 10, ts: "2026-08-18T05:11:00Z", actor: "claude", type: "question", body: "Is 3.13-only acceptable?" },
        ],
      }),
    );
    openComposer();

    expect(screen.getByText("1. Is 3.13-only acceptable?")).toBeVisible();
    expect(screen.getByLabelText("Your answer")).toBeVisible();
  });
});

/** Escape a label for use inside an accessible-name regex. */
function escape(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
