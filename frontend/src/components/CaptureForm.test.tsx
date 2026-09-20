import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import type { DispatchStarted, Task } from "../api/generated";
import type { DispatchStateView } from "../api/types";
import { CaptureForm } from "./CaptureForm";

function createdTask(): Task {
  return {
    schema: 2,
    id: "task-123-created",
    title: "Create from browser",
    created: "2026-08-13T08:00:00Z",
    updated: "2026-08-13T08:00:00Z",
    lifecycle: "ready",
    ball: "agent",
    ball_reason: "available",
    display_status: "Ready",
    priority: "high",
    category: "ux",
    assignment: { eligible: [] },
    spec: {
      summary: "A complete summary.",
      description: "A complete working description.",
    },
  };
}

function renderForm(options: { startExpanded?: boolean } = {}) {
  const onSubmit = vi.fn().mockResolvedValue(createdTask());
  // The drafting control asks the server whether a model is configured, so the form
  // needs a query client. The default mock answers `unconfigured`, which is what every
  // test below except the drafting ones is asserting the form behaves under.
  const queryClient = new QueryClient({
    defaultOptions: { mutations: { retry: false }, queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <CaptureForm
          context={{ route: "/p/inbox/tasks", projectId: "inbox", taskId: null }}
          destinations={[{ id: "inbox", name: "Inbox", reporter: "Jeff Posey" }]}
          existingTaskIds={["task-parent"]}
          onSubmit={onSubmit}
          onFiled={vi.fn()}
          {...options}
        />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return onSubmit;
}

describe("CaptureForm", () => {
  it("opens on the two fields a fifteen-second capture needs, and nothing else", () => {
    renderForm();

    expect(screen.getByRole("textbox", { name: "Title" })).toBeRequired();
    expect(screen.getByRole("textbox", { name: /^What happened/ })).toBeRequired();
    expect(screen.getByRole("checkbox", { name: /Ready for an agent/ })).not.toBeChecked();
    // The specification is in the document -- an unmounted input would lose whatever
    // was typed into it -- but out of reach until it is asked for.
    expect(screen.queryByRole("textbox", { name: "Intent" })).toBeNull();
    expect(screen.getByRole("button", { name: "Add the full specification" })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
  });

  it("files a quick capture without the reporter touching the specification", async () => {
    const onSubmit = renderForm();
    fireEvent.change(screen.getByRole("textbox", { name: "Title" }), {
      target: { value: "The log timestamps are unreadable" },
    });
    fireEvent.change(screen.getByRole("textbox", { name: /^What happened/ }), {
      target: { value: "Every entry shows a full locale string." },
    });
    fireEvent.click(screen.getByRole("button", { name: "File it" }));

    await waitFor(() => expect(onSubmit).toHaveBeenCalledOnce());
    const [projectId, request] = onSubmit.mock.calls[0] ?? [];
    expect(projectId).toBe("inbox");
    // Exactly the record the floating Report issue button produced: tagged, drafted,
    // carrying the route, and carrying nothing the person did not write.
    expect(request).toMatchObject({
      title: "The log timestamps are unreadable",
      lifecycle: "draft",
      tags: ["reported-issue"],
      actor: "Jeff Posey",
      dependencies: [],
    });
    expect(request.description).toContain("Every entry shows a full locale string.");
    expect(request.description).toContain("/p/inbox/tasks");
    expect(request.summary).toBeUndefined();
    expect(request.acceptance).toBeUndefined();
  });

  it("expands to every field the create page offered, and files them in one request", async () => {
    const onSubmit = renderForm();
    fireEvent.change(screen.getByRole("textbox", { name: "Title" }), {
      target: { value: "Create from browser" },
    });
    fireEvent.change(screen.getByRole("textbox", { name: /^What happened/ }), {
      target: { value: "A complete working description." },
    });
    fireEvent.click(screen.getByRole("checkbox", { name: /Ready for an agent/ }));

    fireEvent.click(screen.getByRole("button", { name: "Add the full specification" }));
    fireEvent.change(screen.getByRole("textbox", { name: /^Summary/ }), {
      target: { value: "A complete summary." },
    });
    fireEvent.change(screen.getByRole("textbox", { name: "Intent" }), {
      target: { value: "No terminal required." },
    });
    fireEvent.change(screen.getByRole("textbox", { name: /Read-first context/ }), {
      target: { value: "src/agentjobs/manager.py | Owns creation" },
    });
    fireEvent.change(screen.getByRole("combobox", { name: "Priority" }), {
      target: { value: "high" },
    });
    fireEvent.change(screen.getByRole("textbox", { name: "Category" }), {
      target: { value: "ux" },
    });
    fireEvent.change(screen.getByRole("combobox", { name: /Parent task/ }), {
      target: { value: "task-parent" },
    });
    fireEvent.change(screen.getByRole("textbox", { name: /Acceptance criteria/ }), {
      target: { value: "Appears in the task list\nStarts ready" },
    });
    fireEvent.change(screen.getByRole("textbox", { name: /Dependencies/ }), {
      target: { value: "task-first | Supplies the API" },
    });
    // The prefilled population tag is the author's to drop when this is not a finding.
    fireEvent.change(screen.getByRole("textbox", { name: /^Tags/ }), {
      target: { value: "gui, testing" },
    });
    fireEvent.click(screen.getByRole("button", { name: "File it" }));

    await waitFor(() => expect(onSubmit).toHaveBeenCalledOnce());
    expect(onSubmit.mock.calls[0]?.[1]).toMatchObject({
      title: "Create from browser",
      summary: "A complete summary.",
      lifecycle: "ready",
      priority: "high",
      category: "ux",
      intent: "No terminal required.",
      parent: "task-parent",
      tags: ["gui", "testing"],
      context: [{ path: "src/agentjobs/manager.py", why: "Owns creation" }],
      acceptance: [
        { id: "ac-1", text: "Appears in the task list", status: "pending" },
        { id: "ac-2", text: "Starts ready", status: "pending" },
      ],
      dependencies: [{ task: "task-first", type: "needs", note: "Supplies the API" }],
    });
  });

  it("keeps what was typed into the specification when it is collapsed again", async () => {
    const onSubmit = renderForm();
    fireEvent.change(screen.getByRole("textbox", { name: "Title" }), {
      target: { value: "Still complete" },
    });
    fireEvent.change(screen.getByRole("textbox", { name: /^What happened/ }), {
      target: { value: "The body." },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add the full specification" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Intent" }), {
      target: { value: "Worth keeping." },
    });
    fireEvent.click(screen.getByRole("button", { name: "Hide the full specification" }));
    fireEvent.click(screen.getByRole("button", { name: "File it" }));

    await waitFor(() => expect(onSubmit).toHaveBeenCalledOnce());
    expect(onSubmit.mock.calls[0]?.[1].intent).toBe("Worth keeping.");
  });

  it("explains malformed context without sending a partial task", async () => {
    const onSubmit = renderForm({ startExpanded: true });
    fireEvent.change(screen.getByRole("textbox", { name: "Title" }), {
      target: { value: "Create from browser" },
    });
    fireEvent.change(screen.getByRole("textbox", { name: /^What happened/ }), {
      target: { value: "A complete working description." },
    });
    fireEvent.change(screen.getByRole("textbox", { name: /Read-first context/ }), {
      target: { value: "missing reason" },
    });
    fireEvent.click(screen.getByRole("button", { name: "File it" }));

    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText("Context line 1 must use path | why.")).toBeVisible();
    expect(onSubmit).not.toHaveBeenCalled();
  });
});

/**
 * Starting an agent on what you are filing (task-176).
 *
 * The rule under all of it: the box is off unless somebody turned it on, this visit,
 * on this capture.
 */
describe("CaptureForm: start an agent on it now", () => {
  const boxName = /Start an agent on it now/;

  /** Every dispatch gate open. */
  function openGates(overrides: Partial<DispatchStateView> = {}): DispatchStateView {
    return {
      project_id: "inbox",
      configured: true,
      master_enabled: true,
      project_enabled: true,
      can_dispatch: true,
      config_path: "~/.agentjobs/dispatch.yaml",
      ...overrides,
    } as DispatchStateView;
  }

  function started(runId = "run_1"): DispatchStarted {
    return {
      run_id: runId,
      mode: "session",
      posture: "auto",
      task_id: "task-123-created",
      caused_by: 2,
    } as DispatchStarted;
  }

  function renderWithDispatch({
    dispatchState = null as DispatchStateView | null,
    onStart = vi.fn().mockResolvedValue(started()),
    onSubmit = vi.fn().mockResolvedValue(createdTask()),
    onFiled = vi.fn(),
    reporter = "Jeff Posey" as string | null,
  } = {}) {
    const queryClient = new QueryClient({
      defaultOptions: { mutations: { retry: false }, queries: { retry: false } },
    });
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <CaptureForm
            context={{ route: "/p/inbox/tasks", projectId: "inbox", taskId: null }}
            destinations={[{ id: "inbox", name: "Inbox", reporter }]}
            existingTaskIds={[]}
            onSubmit={onSubmit}
            onFiled={onFiled}
            dispatchState={dispatchState}
            onStart={onStart}
          />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    return { onSubmit, onStart, onFiled };
  }

  function fillTheCore() {
    fireEvent.change(screen.getByRole("textbox", { name: "Title" }), {
      target: { value: "Filters match nothing" },
    });
    fireEvent.change(screen.getByRole("textbox", { name: /^What happened/ }), {
      target: { value: "Every task-list filter returns zero rows." },
    });
  }

  it("is present and unchecked once the capture is marked ready for an agent", () => {
    renderWithDispatch({ dispatchState: openGates() });
    fireEvent.click(screen.getByRole("checkbox", { name: /Ready for an agent/ }));

    const box = screen.getByRole("checkbox", { name: boxName });
    expect(box).toBeInTheDocument();
    expect(box).not.toBeChecked();
    expect(box).toBeEnabled();
  });

  it("is closed on a draft, and names that as the reason", () => {
    renderWithDispatch({ dispatchState: openGates() });

    // "Ready for an agent" is off by default, so this is too -- and says why.
    expect(screen.getByRole("checkbox", { name: boxName })).toBeDisabled();
    expect(screen.getByText(/being filed as a draft/)).toBeVisible();
  });

  it("names the gate that is shut when this machine cannot dispatch", () => {
    renderWithDispatch({
      dispatchState: openGates({
        can_dispatch: false,
        refusal: { reason: "project_not_enabled", message: "inbox is not enabled for dispatch." },
      }),
    });
    fireEvent.click(screen.getByRole("checkbox", { name: /Ready for an agent/ }));

    expect(screen.getByRole("checkbox", { name: boxName })).toBeDisabled();
    expect(screen.getByText(/inbox is not enabled for dispatch/)).toBeVisible();
    // The code, not just the sentence: which gate is shut has to be readable without
    // matching prose, exactly as on every other dispatch surface.
    expect(document.querySelector('[data-refusal-reason="project_not_enabled"]')).toBeInTheDocument();
  });

  it("is closed while the dispatch state is still unknown, rather than enabled and hopeful", () => {
    renderWithDispatch({ dispatchState: null });
    fireEvent.click(screen.getByRole("checkbox", { name: /Ready for an agent/ }));

    expect(screen.getByRole("checkbox", { name: boxName })).toBeDisabled();
  });

  it("is closed when the project configures no human to attribute a run to", () => {
    renderWithDispatch({ dispatchState: openGates(), reporter: null });
    fireEvent.click(screen.getByRole("checkbox", { name: /Ready for an agent/ }));

    expect(screen.getByRole("checkbox", { name: boxName })).toBeDisabled();
    expect(document.querySelector('[data-refusal-reason="no_signed_in_user"]')).toBeInTheDocument();
  });

  it("files, then starts, and reports both outcomes as one", async () => {
    const { onSubmit, onStart, onFiled } = renderWithDispatch({ dispatchState: openGates() });
    fireEvent.click(screen.getByRole("checkbox", { name: /Ready for an agent/ }));
    fillTheCore();
    fireEvent.click(screen.getByRole("checkbox", { name: boxName }));
    fireEvent.click(screen.getByRole("button", { name: "File it" }));

    await waitFor(() => expect(onFiled).toHaveBeenCalledOnce());
    expect(onSubmit).toHaveBeenCalledOnce();
    expect(onStart).toHaveBeenCalledWith("inbox", "task-123-created");
    expect(onFiled.mock.calls[0]?.[1]).toEqual({
      task: createdTask(),
      start: { kind: "started", runId: "run_1" },
    });
  });

  it("keeps the task and reports the refusal when the start is refused", async () => {
    const onStart = vi.fn().mockRejectedValue({
      code: "dirty_tree",
      message: "C:/projects/agentjobs has uncommitted changes (src/one.py).",
      suggested_action: "Commit or stash the working tree, then dispatch.",
    });
    const { onFiled } = renderWithDispatch({ dispatchState: openGates(), onStart });
    fireEvent.click(screen.getByRole("checkbox", { name: /Ready for an agent/ }));
    fillTheCore();
    fireEvent.click(screen.getByRole("checkbox", { name: boxName }));
    fireEvent.click(screen.getByRole("button", { name: "File it" }));

    // The create succeeded, so the form reports a filed task -- with the start refused
    // beside it, under the gate's own code. Nothing is rolled back and no error is
    // shown in place of the receipt.
    await waitFor(() => expect(onFiled).toHaveBeenCalledOnce());
    const outcome = onFiled.mock.calls[0]?.[1];
    expect(outcome.task).toEqual(createdTask());
    expect(outcome.start.kind).toBe("refused");
    expect(outcome.start.refusal.reason).toBe("dirty_tree");
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("starts nothing when the box was left alone, which is how it arrives", async () => {
    const { onStart, onFiled } = renderWithDispatch({ dispatchState: openGates() });
    fireEvent.click(screen.getByRole("checkbox", { name: /Ready for an agent/ }));
    fillTheCore();
    fireEvent.click(screen.getByRole("button", { name: "File it" }));

    await waitFor(() => expect(onFiled).toHaveBeenCalledOnce());
    expect(onStart).not.toHaveBeenCalled();
    expect(onFiled.mock.calls[0]?.[1].start).toEqual({ kind: "not_asked" });
  });

  it("unchecks itself when the capture is put back to a draft", async () => {
    const { onStart, onFiled } = renderWithDispatch({ dispatchState: openGates() });
    fireEvent.click(screen.getByRole("checkbox", { name: /Ready for an agent/ }));
    fireEvent.click(screen.getByRole("checkbox", { name: boxName }));
    expect(screen.getByRole("checkbox", { name: boxName })).toBeChecked();

    fireEvent.click(screen.getByRole("checkbox", { name: /Ready for an agent/ }));
    expect(screen.getByRole("checkbox", { name: boxName })).not.toBeChecked();

    fillTheCore();
    fireEvent.click(screen.getByRole("button", { name: "File it" }));
    await waitFor(() => expect(onFiled).toHaveBeenCalledOnce());
    expect(onStart).not.toHaveBeenCalled();
  });

  it("tells the caller which project to read the gates of", () => {
    const onDestinationChange = vi.fn();
    const queryClient = new QueryClient({
      defaultOptions: { mutations: { retry: false }, queries: { retry: false } },
    });
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <CaptureForm
            context={{ route: "/", projectId: null, taskId: null }}
            destinations={[
              { id: "inbox", name: "Inbox", reporter: "Jeff Posey" },
              { id: "other", name: "Other", reporter: "Jeff Posey" },
            ]}
            existingTaskIds={[]}
            onSubmit={vi.fn()}
            onFiled={vi.fn()}
            onDestinationChange={onDestinationChange}
          />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    // The fallback destination on mount, so the gates read are the ones that apply.
    expect(onDestinationChange).toHaveBeenCalledWith("inbox");
    fireEvent.change(screen.getByRole("combobox", { name: /File into/ }), {
      target: { value: "other" },
    });
    expect(onDestinationChange).toHaveBeenCalledWith("other");
  });
});
