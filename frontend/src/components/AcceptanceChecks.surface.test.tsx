import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { TaskRead } from "../api/types";
import { buildCaptureRequest } from "../report/issueReport";
import { TaskFields, type TaskFieldsPatch } from "./TaskFields";

afterEach(cleanup);

/**
 * The browser may run a check; it may not write one (task-152, criterion sc-4).
 *
 * A `check` is an argv this machine executes. A surface that could write one is a
 * surface that could start an arbitrary process here under somebody else's hand, which
 * is the same argument that keeps a runner's argv out of the browser and the reason
 * task-147 filed `POST .../check` under the dispatch capability rather than under the
 * `task.*` ones a run already holds.
 *
 * "No way to write one" is only worth asserting **against the requests the page can
 * actually produce**. A test that read the JSX and found no input labelled "check"
 * would pass while a field elsewhere posted one. So each case here drives a real
 * control and inspects the body it built.
 *
 * Three bodies, which between them are every task-content write the app makes:
 *
 *   1. Create, via the capture form. It *can* write criteria -- one per typed line --
 *      and each is text and a status, never a command.
 *   2. Edit, via the task page's field form. Its patch has no acceptance at all.
 *   3. Run, via `POST .../check`. It has no body; that case is in
 *      `AcceptanceChecks.test.tsx`, which asserts the handler is called with no
 *      argument, and `App.check.test.tsx`, which asserts the request carries a path
 *      and nothing else.
 *
 * Browser editing of list fields is task-382. It inherits this: whatever it opens for
 * editing, `check` is not part of it, and this file is what fails if it becomes so.
 */

const CONTEXT = { route: "/p/agentjobs/tasks", projectId: "agentjobs", taskId: null };

describe("creating a task from the browser", () => {
  it("turns typed acceptance lines into text, never into a command", () => {
    const request = buildCaptureRequest({
      draft: { title: "A captured issue", details: "What went wrong.", actionable: true },
      context: CONTEXT,
      destinationProjectId: "agentjobs",
      reporter: "Jeff Posey",
      operationId: "op-1",
      spec: {
        summary: "Summary.",
        intent: "",
        constraints: "",
        out_of_scope: "",
        // A line a person might type hoping it would be executed. It is a criterion's
        // text and nothing runs it.
        acceptance: ["The gate is green", "poetry run pytest -q"],
      },
    });

    expect(request.acceptance).toEqual([
      { id: "ac-1", text: "The gate is green", status: "pending" },
      { id: "ac-2", text: "poetry run pytest -q", status: "pending" },
    ]);
    for (const criterion of request.acceptance ?? []) {
      expect(Object.keys(criterion).sort()).toEqual(["id", "status", "text"]);
    }
  });
});

describe("editing a task from the browser", () => {
  const task = {
    schema: 2,
    id: "task-152",
    title: "Check results on the task page",
    created: "2026-09-22T08:00:00Z",
    updated: "2026-09-22T09:00:00Z",
    lifecycle: "active",
    ball: "agent",
    ball_reason: "work",
    ball_prompt: "Execute the spec.",
    display_status: "Being worked",
    priority: "medium",
    category: "ux",
    tags: ["ux"],
    effort: "half a day",
    spec: { summary: "Summary.", description: "Description." },
    acceptance: [
      { id: "ac-1", text: "The gate is green.", check: ["poetry", "run", "pytest", "-q"], status: "met" },
    ],
    log: [],
  } as unknown as TaskRead;

  it("sends a patch that cannot carry acceptance, so no edit can reach a check", async () => {
    const onSave = vi.fn(async (_patch: TaskFieldsPatch) => undefined);
    render(
      <TaskFields
        task={task}
        identity={{ ok: true, user: "Jeff Posey", problem: null, detail: "" }}
        onSave={onSave}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /edit/i }));
    // Nothing in the opened form holds the command, so there is not even a control to
    // submit it back with.
    for (const field of Array.from(document.querySelectorAll("input, textarea"))) {
      expect((field as HTMLInputElement).value).not.toContain("pytest");
    }

    fireEvent.change(screen.getByLabelText(/title/i), { target: { value: "A new title" } });
    fireEvent.click(screen.getByRole("button", { name: /save/i }));

    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    const patch = onSave.mock.calls[0]![0];
    expect(patch).not.toHaveProperty("acceptance");
    expect(JSON.stringify(patch)).not.toContain("pytest");
  });
});
