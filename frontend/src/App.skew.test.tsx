import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it } from "vitest";

import type { DashboardResponse, TaskDetailResponse, TaskRead } from "./api/types";
import { client } from "./api/generated/client.gen";
import { App } from "./App";
import { apiMockServer, VERSION_IN_STEP } from "./test/api-mock";
import { setViewport } from "./test/viewport";

/**
 * Version skew, rendered by the whole application rather than by the banner alone.
 *
 * The claim these make is the one a component test cannot: that a page which has
 * detected skew is **still a working page**. The banner is deliberately not a modal,
 * because a stale server is usually still mostly usable and locking somebody out of
 * their tracker to tell them about a version mismatch trades one problem for a worse
 * one. Asserting that the task record renders beside the warning is what keeps a later
 * change from quietly turning this into a blocking dialog.
 */

const OTHER_CONTRACT = "f".repeat(64);

function task(id: string, overrides: Partial<TaskRead> = {}): TaskRead {
  return {
    schema: 2,
    id,
    title: `Title of ${id}`,
    created: "2026-08-13T08:00:00Z",
    updated: "2026-08-13T09:00:00Z",
    lifecycle: "ready",
    ball: "agent",
    ball_reason: "available",
    display_status: "Ready",
    priority: "high",
    category: "general",
    tags: [],
    assignment: { eligible: [] },
    spec: { summary: `Summary of ${id}`, description: "Body." },
    ...overrides,
  };
}

const TASKS = [task("task-001"), task("task-002")];

const DETAIL: TaskDetailResponse = {
  task: task("task-001", { lifecycle: "active", ball: "agent", ball_reason: "work", ball_prompt: "Carry on." }),
  parent_task: null,
  children: [],
  needs: [],
  blocks: [],
  related: [],
  child_dependency_edges: [],
  identity: { ok: true, user: "Jeff Posey", problem: null, detail: "Acting as Jeff Posey." },
};

const DASHBOARD: DashboardResponse = {
  stats: { total: 2, in_progress: 0, blocked: 0, waiting_for_human: 0, awaiting_input: 0, completed: 0 },
  active_tasks: [],
  recent_updates: [],
  waiting_tasks: [],
  backlog_tasks: [],
  next_task: null,
  queue_preview: [],
  next_action: "nothing_claimable",
  broken_files: [],
  identity: { ok: true, user: "jeff", problem: null, detail: "Acting as jeff." },
};

function handlers() {
  return [
    http.get("*/api/projects", () =>
      HttpResponse.json([
        { id: "inbox", name: "Inbox", root: "C:/projects/inbox", task_count: TASKS.length, tasks_directory: "C:/projects/inbox/tasks", default_user: "jeff" },
      ]),
    ),
    http.get("*/api/projects/inbox/tasks", () => HttpResponse.json(TASKS)),
    http.get("*/api/projects/inbox/tasks/broken", () => HttpResponse.json([])),
    http.get("*/api/projects/inbox/queue", () =>
      HttpResponse.json({ bands: [], problems: [], repair_command: "agentjobs queue repair" }),
    ),
    http.get("*/api/projects/inbox/tasks/task-001/detail", () => HttpResponse.json(DETAIL)),
    http.get("*/api/projects/inbox/dashboard", () => HttpResponse.json(DASHBOARD)),
    http.get("*/api/projects/inbox/dispatch", () =>
      HttpResponse.json({ enabled: false, runners: [], groups: [], reason: "Dispatch is off in this fixture." }),
    ),
    http.get("*/api/projects/inbox/dispatch/runs", () => HttpResponse.json([])),
    http.get("*/api/projects/inbox/dispatch/finishes/task-001", () => HttpResponse.json(null)),
    http.get("*/api/projects/inbox/revision", () => HttpResponse.json({ revision: "skew-test", task_count: TASKS.length })),
    http.get("*/api/projects/inbox/attention", () => HttpResponse.json({ blocking: 0 })),
  ];
}

function renderApp() {
  const queryClient = new QueryClient({
    defaultOptions: { mutations: { retry: false }, queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/p/inbox/tasks/task-001"]}>
        <App />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return queryClient;
}

beforeEach(() => {
  client.setConfig({ baseUrl: "http://localhost" });
  setViewport(1280, 800);
  apiMockServer.use(...handlers());
});

describe("an app whose bundle is out of step with its server", () => {
  it("warns beside the task record instead of in place of it", async () => {
    apiMockServer.use(
      http.get("*/api/version", () => HttpResponse.json({ ...VERSION_IN_STEP, api_digest: OTHER_CONTRACT })),
    );
    renderApp();

    expect(await screen.findByRole("region", { name: "Task detail" })).toBeVisible();
    expect(await screen.findByRole("alert")).toHaveTextContent("disagree about the API");
    // Not a dialog and not a focus trap: nothing here takes the page away.
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("renders exactly as before when the version endpoint cannot be reached", async () => {
    apiMockServer.use(http.get("*/api/version", () => HttpResponse.error()));
    const queryClient = renderApp();

    expect(await screen.findByRole("region", { name: "Task detail" })).toBeVisible();
    await waitFor(() => expect(queryClient.isFetching()).toBe(0));
    expect(screen.queryByRole("alert")).toBeNull();
  });
});
