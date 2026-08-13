import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import type { Task, TaskCreateRequest, TaskRead } from "./api/generated";
import { client } from "./api/generated/client.gen";
import { App } from "./App";
import { apiMockServer } from "./test/api-mock";

function renderApp() {
  const queryClient = new QueryClient({
    defaultOptions: { mutations: { retry: false }, queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/p/inbox/tasks/new"]}>
        <App />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("generated client at the HTTP boundary", () => {
  it("creates a ready task and then renders it from the list response", async () => {
    client.setConfig({ baseUrl: "http://localhost" });
    let created: TaskRead | null = null;
    let received: TaskCreateRequest | null = null;
    apiMockServer.use(
      http.get("*/api/projects/inbox/tasks", () => HttpResponse.json(created ? [created] : [])),
      http.get("*/api/projects/inbox/tasks/broken", () => HttpResponse.json([])),
      http.get("*/api/projects/inbox/revision", () => HttpResponse.json({ revision: "test-revision", task_count: created ? 1 : 0 })),
      http.post("*/api/projects/inbox/tasks", async ({ request }) => {
        received = await request.json() as TaskCreateRequest;
        const task: Task = {
          schema: 2,
          id: "task-123-network-boundary",
          title: received.title,
          created: "2026-08-13T21:00:00Z",
          updated: "2026-08-13T21:00:00Z",
          lifecycle: received.lifecycle ?? "draft",
          ball: received.lifecycle === "ready" ? "agent" : "human",
          ball_reason: received.lifecycle === "ready" ? "available" : "spec",
          ball_prompt: received.lifecycle === "ready" ? null : "Finish specifying this task.",
          display_status: received.lifecycle === "ready" ? "Ready" : "Draft",
          priority: received.priority ?? "medium",
          category: received.category ?? "general",
          assignment: { eligible: [] },
          spec: {
            summary: received.summary ?? received.title,
            description: received.description,
          },
        };
        created = { ...task, actionable: true, unmet_needs: [], needs_cycles: [], unblocks_count: 0, open_children_count: 0 };
        return HttpResponse.json(task, { status: 201 });
      }),
    );
    renderApp();

    fireEvent.change(await screen.findByRole("textbox", { name: "Title" }), { target: { value: "Created through HTTP" } });
    fireEvent.change(screen.getByRole("textbox", { name: /Summary/ }), { target: { value: "Exercises the real generated client." } });
    fireEvent.change(screen.getByRole("textbox", { name: /Working description/ }), { target: { value: "Intercept the request at HTTP, not by replacing the client." } });
    fireEvent.click(screen.getByRole("radio", { name: /Ready/ }));
    fireEvent.click(screen.getByRole("button", { name: "Create task" }));

    const tasks = await screen.findByRole("region", { name: "Tasks" });
    expect(within(tasks).getByText("Created through HTTP")).toBeVisible();
    expect(within(tasks).getByText("Actionable now")).toBeVisible();
    await waitFor(() => expect(received).toEqual(expect.objectContaining({
      title: "Created through HTTP",
      lifecycle: "ready",
      summary: "Exercises the real generated client.",
    })));
  });
});
