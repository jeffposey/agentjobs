import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import type { Task } from "../api/generated";
import { TaskCreate } from "./TaskCreate";

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

function renderForm(onCreate = vi.fn().mockResolvedValue(createdTask())) {
  render(
    <MemoryRouter>
      <TaskCreate projectId="inbox" existingTaskIds={["task-parent"]} onCreate={onCreate} />
    </MemoryRouter>,
  );
  return onCreate;
}

describe("TaskCreate", () => {
  it("requires the resumption core and makes the starting holder explicit", () => {
    renderForm();

    expect(screen.getByRole("textbox", { name: "Title" })).toBeRequired();
    expect(screen.getByRole("textbox", { name: /Summary/ })).toBeRequired();
    expect(screen.getByRole("textbox", { name: /Working description/ })).toBeRequired();
    expect(screen.getByRole("radio", { name: /Draft/ })).toBeChecked();
    expect(screen.getByRole("radio", { name: /Ready/ })).not.toBeChecked();
    expect(screen.getByText("More specification").closest("details")).not.toHaveAttribute("open");
  });

  it("submits the core, disclosed spec, and relationships through one create request", async () => {
    const onCreate = renderForm();
    fireEvent.change(screen.getByRole("textbox", { name: "Title" }), { target: { value: "Create from browser" } });
    fireEvent.change(screen.getByRole("textbox", { name: /Summary/ }), { target: { value: "A complete summary." } });
    fireEvent.change(screen.getByRole("textbox", { name: /Working description/ }), { target: { value: "A complete working description." } });
    fireEvent.click(screen.getByRole("radio", { name: /Ready/ }));

    fireEvent.click(screen.getByText("More specification"));
    fireEvent.change(screen.getByRole("textbox", { name: "Intent" }), { target: { value: "No terminal required." } });
    fireEvent.change(screen.getByRole("textbox", { name: /Read-first context/ }), { target: { value: "src/agentjobs/manager.py | Owns creation" } });

    fireEvent.click(screen.getByText("Planning and relationships"));
    fireEvent.change(screen.getByRole("combobox", { name: "Priority" }), { target: { value: "high" } });
    fireEvent.change(screen.getByRole("textbox", { name: "Category" }), { target: { value: "ux" } });
    fireEvent.change(screen.getByRole("combobox", { name: /Parent task/ }), { target: { value: "task-parent" } });
    fireEvent.change(screen.getByRole("textbox", { name: /Acceptance criteria/ }), { target: { value: "Appears in the task list\nStarts ready" } });
    fireEvent.change(screen.getByRole("textbox", { name: /Dependencies/ }), { target: { value: "task-first | Supplies the API" } });
    fireEvent.click(screen.getByRole("button", { name: "Create task" }));

    await waitFor(() => expect(onCreate).toHaveBeenCalledOnce());
    expect(onCreate).toHaveBeenCalledWith(expect.objectContaining({
      title: "Create from browser",
      summary: "A complete summary.",
      description: "A complete working description.",
      lifecycle: "ready",
      priority: "high",
      category: "ux",
      intent: "No terminal required.",
      parent: "task-parent",
      context: [{ path: "src/agentjobs/manager.py", why: "Owns creation" }],
      acceptance: [
        { id: "ac-1", text: "Appears in the task list", status: "pending" },
        { id: "ac-2", text: "Starts ready", status: "pending" },
      ],
      dependencies: [{ task: "task-first", type: "needs", note: "Supplies the API" }],
    }));
  });

  it("explains malformed context without sending a partial task", async () => {
    const onCreate = renderForm();
    fireEvent.change(screen.getByRole("textbox", { name: "Title" }), { target: { value: "Create from browser" } });
    fireEvent.change(screen.getByRole("textbox", { name: /Summary/ }), { target: { value: "A complete summary." } });
    fireEvent.change(screen.getByRole("textbox", { name: /Working description/ }), { target: { value: "A complete working description." } });
    fireEvent.click(screen.getByText("More specification"));
    fireEvent.change(screen.getByRole("textbox", { name: /Read-first context/ }), { target: { value: "missing reason" } });
    fireEvent.click(screen.getByRole("button", { name: "Create task" }));

    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText("Context line 1 must use path | why.")).toBeVisible();
    expect(onCreate).not.toHaveBeenCalled();
  });
});
