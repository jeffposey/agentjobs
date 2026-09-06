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

/**
 * There is nothing to open (task-017, second pass).
 *
 * The questions were behind an "Answer Questions" button for one release. Jeff:
 * *"It should not require pressing answer questions button to get the multiple choice
 * question prompt, that should be in default view."* Every test below therefore renders
 * and asserts, with no gesture in between -- which is the property, not a convenience.
 */
function submit() {
  return screen.getByRole("button", { name: "✓ Send answers" });
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

    // Four questions, all of which offer options, and all of which can still be
    // answered in words. This is the constraint task-017 is most insistent about.
    expect(screen.getAllByLabelText("Something else")).toHaveLength(4);
    // The one whose answer is a number says so, rather than making the reader guess.
    expect(screen.getByPlaceholderText("a number of minutes")).toBeVisible();
  });

  it("sends the tapped options as answers threaded to their questions", async () => {
    const actions = renderPanel();

    fireEvent.click(screen.getByRole("button", { name: /Session mode primary/ }));
    fireEvent.click(screen.getByRole("button", { name: /No -- re-attach/ }));
    fireEvent.click(submit());

    await waitFor(() => expect(actions.onSendBack).toHaveBeenCalled());
    expect(sent(actions).reason).toBe("answer");
    expect(answersFrom(actions)).toEqual([
      { re: 10, selected: ["Session mode primary, batch retained"], other: null },
      { re: 11, selected: ["No -- re-attach"], other: null },
    ]);
  });

  it("can be submitted with nothing typed at all", async () => {
    const actions = renderPanel();

    // The whole point: four taps on a phone, no keyboard. Submit is live once one
    // option is chosen, and the prose field stays empty.
    fireEvent.click(screen.getByRole("button", { name: /Batch-only/ }));
    fireEvent.click(submit());

    await waitFor(() => expect(actions.onSendBack).toHaveBeenCalled());
    expect(sent(actions).feedback).toBe("");
    expect(answersFrom(actions)).toEqual([{ re: 10, selected: ["Batch-only"], other: null }]);
  });

  it("refuses to submit until something has been said", () => {
    renderPanel();

    expect(submit()).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: /4 hours/ }));
    expect(submit()).toBeEnabled();
  });

  it("carries free text that rejects every option offered", async () => {
    const actions = renderPanel();

    // Jeff's actual answer to question 3 on 2026-08-18: none of 15 minutes, 4 hours or
    // the hard kill. A form that could not capture this would be worse than a prose box.
    fireEvent.change(screen.getByPlaceholderText("a number of minutes"), {
      target: { value: "60 minutes" },
    });
    fireEvent.click(submit());

    await waitFor(() => expect(actions.onSendBack).toHaveBeenCalled());
    expect(answersFrom(actions)).toEqual([{ re: 12, selected: [], other: "60 minutes" }]);
  });

  it("carries an option and free text together", async () => {
    const actions = renderPanel();

    fireEvent.click(screen.getByRole("button", { name: /Keep the rule/ }));
    fireEvent.change(within(screen.getByText("2. Kill runs when the supervisor restarts?").closest("fieldset")!).getByLabelText("Something else"), {
      target: { value: "but log it loudly" },
    });
    fireEvent.click(submit());

    await waitFor(() => expect(actions.onSendBack).toHaveBeenCalled());
    expect(answersFrom(actions)).toEqual([
      { re: 11, selected: ["Keep the rule"], other: "but log it loudly" },
    ]);
  });

  it("takes several options only where the question said it could", async () => {
    const actions = renderPanel();

    fireEvent.click(screen.getByRole("button", { name: /Rescope task-070/ }));
    fireEvent.click(screen.getByRole("button", { name: /Rescope task-072/ }));
    // A single-select question replaces rather than accumulates, so the second tap on
    // question 1 leaves one answer, not two.
    fireEvent.click(screen.getByRole("button", { name: /Session-only/ }));
    fireEvent.click(screen.getByRole("button", { name: /Batch-only/ }));
    fireEvent.click(submit());

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

    const option = screen.getByRole("button", { name: /Batch-only/ });
    fireEvent.click(option);
    expect(option).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(option);
    expect(option).toHaveAttribute("aria-pressed", "false");
    expect(submit()).toBeDisabled();
  });

  it("answers all four of task-077's questions in one submit", async () => {
    const actions = renderPanel();

    fireEvent.click(screen.getByRole("button", { name: /Session mode primary/ }));
    fireEvent.click(screen.getByRole("button", { name: /No -- re-attach/ }));
    fireEvent.change(screen.getByPlaceholderText("a number of minutes"), {
      target: { value: "60 minutes" },
    });
    fireEvent.click(screen.getByRole("button", { name: /Rescope task-070/ }));
    fireEvent.click(screen.getByRole("button", { name: /Rescope task-072/ }));
    fireEvent.click(submit());

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

    expect(screen.queryByPlaceholderText("a number of minutes")).not.toBeInTheDocument();
    expect(screen.getAllByLabelText("Something else")).toHaveLength(3);
  });

  it("does not offer to open what is already open", () => {
    renderPanel();

    const panel = screen.getByRole("region", { name: "Review actions" });
    // A button that reveals the form the reader is looking at is the same verb twice.
    expect(within(panel).queryByRole("button", { name: "✎ Answer Questions" })).not.toBeInTheDocument();
    // The verbs that are still distinct acts stay exactly where they were.
    expect(within(panel).getByRole("button", { name: "↪ New Instructions" })).toBeVisible();
    expect(within(panel).getByRole("button", { name: "⏸ Hold" })).toBeVisible();
  });

  it("falls back to the prose composer when the task has no open questions", () => {
    renderPanel(task({ log: [{ id: 9, ts: "2026-08-18T05:11:00Z", actor: "claude", type: "handoff", body: "Decide." }] }));

    // Nothing is asked, so nothing renders by itself and the button is the way in --
    // exactly the behaviour task-231 shipped, unchanged.
    expect(screen.queryByRole("button", { name: "✓ Send answers" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "✎ Answer Questions" }));
    expect(screen.getByLabelText("Your answer")).toBeRequired();
  });

  it("renders the questions on a review task without disturbing Approve", () => {
    renderPanel(task({ ball_reason: "review", display_status: "Waiting for review" }));

    const panel = screen.getByRole("region", { name: "Review actions" });
    // Approve stays, because reviewing is still what the task is for, and the questions
    // are answerable without choosing between the two.
    expect(within(panel).getByRole("button", { name: /Approve/ })).toBeVisible();
    expect(within(panel).getByRole("button", { name: "✎ Request Changes" })).toBeVisible();
    expect(within(panel).getAllByLabelText("Something else")).toHaveLength(4);
  });

  it("leaves a held task alone, questions or not", () => {
    // Answering hands the ball back, which would lift the hold -- a control that
    // quietly undoes the control beside it.
    renderPanel(task({ ball: "agent", ball_reason: "hold", display_status: "On hold" }));

    const panel = screen.getByRole("region", { name: "Hold actions" });
    expect(within(panel).getByRole("button", { name: /Resume/ })).toBeVisible();
    expect(screen.queryByRole("button", { name: "✓ Send answers" })).not.toBeInTheDocument();
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

    expect(screen.getByText("1. Is 3.13-only acceptable?")).toBeVisible();
    expect(screen.getByLabelText("Your answer")).toBeVisible();
    expect(submit()).toBeDisabled();
  });
});

/**
 * The heading of a question card (task-363).
 *
 * Seen on the task-240 review: "1. 1. Open a task from the sidebar...". The form
 * numbers the cards and the agent had numbered its own questions, so the reader got
 * both. The visual half of that defect -- a two-line heading with the fieldset's top
 * border ruled through it -- cannot be asserted in jsdom, which computes no layout;
 * it was driven in a browser and the evidence is on task-363.
 */
describe("a question heading", () => {
  /**
   * Scoped to the panel, because the log renders the same question bodies further down
   * the page and an unscoped query matches both.
   */
  function heading(text: string) {
    return within(screen.getByRole("region", { name: "Review actions" })).getByText(text);
  }

  function withBodies(...bodies: Array<string>) {
    renderPanel(
      task({
        log: [
          { id: 9, ts: "2026-08-18T05:11:00Z", actor: "claude", type: "handoff", body: "Decide." },
          ...bodies.map((body, index) => ({
            id: 10 + index,
            ts: "2026-08-18T05:11:00Z",
            actor: "claude",
            type: "question" as const,
            body,
          })),
        ],
      }),
    );
  }

  it("numbers a question once when the agent numbered it too", () => {
    withBodies("1. Open a task from the sidebar. Did the list stay put?");

    expect(heading("1. Open a task from the sidebar. Did the list stay put?")).toBeVisible();
    expect(
      within(screen.getByRole("region", { name: "Review actions" })).queryByText(/^1\. 1\./),
    ).toBeNull();
  });

  it("keeps the form's number rather than the body's when the two disagree", () => {
    // The panel shows only the questions with no answer threaded to them, so an
    // agent's own numbering goes stale the moment one of them is answered. Here the
    // agent wrote 2 and 4; the cards are 1 and 2, and 1 and 2 is what a reviewer can
    // point at.
    withBodies("2. Which port?", "4. Keep the sandbox up?");

    expect(heading("1. Which port?")).toBeVisible();
    expect(heading("2. Keep the sandbox up?")).toBeVisible();
  });

  it("strips a bracketed ordinal too", () => {
    withBodies("3) Is the legend clear of the border?");

    expect(heading("1. Is the legend clear of the border?")).toBeVisible();
  });

  it("leaves a number that is part of the question alone", () => {
    // Not an ordinal: no delimiter, or too many digits to be one.
    withBodies("8876 or 8910 for the sandbox?");

    expect(heading("1. 8876 or 8910 for the sandbox?")).toBeVisible();
  });

  it("leaves a body that is nothing but an ordinal alone rather than emptying it", () => {
    withBodies("1.");

    expect(heading("1. 1.")).toBeVisible();
  });

  it("keeps the fieldset grouping, so the options are still announced with the question", () => {
    // Whatever the visual fix, a group of options is a fieldset with a legend.
    withBodies("Which port?");

    expect(screen.getByRole("group", { name: "1. Which port?" })).toBeVisible();
  });
});

/** Escape a label for use inside an accessible-name regex. */
function escape(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
