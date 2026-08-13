import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import type { Task, TaskDetailResponse } from "../api/generated";
import { TaskDetail } from "./TaskDetail";

function task(id: string, overrides: Partial<Task> = {}): Task {
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
  identity: { ok: true, user: "Jeff Posey", problem: null, detail: "" },
};

function renderDetail(value = detail) {
  const actions = {
    onApprove: vi.fn(async () => undefined),
    onRequestChanges: vi.fn(async () => undefined),
    onReject: vi.fn(async () => undefined),
  };
  render(<MemoryRouter><TaskDetail detail={value} projectId="inbox" {...actions} /></MemoryRouter>);
  return actions;
}

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
    await waitFor(() => expect(actions.onRequestChanges).toHaveBeenCalledWith("Tighten the layout."));
  });

  it("blocks every review action when identity is unclear", () => {
    renderDetail({ ...detail, identity: { ok: false, user: null, problem: "multiple", detail: "Cannot choose safely." } });

    expect(screen.getByText("Cannot choose safely.")).toBeVisible();
    expect(screen.queryByRole("button", { name: /Approve/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Request Changes/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Reject/ })).not.toBeInTheDocument();
  });
});
