import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it } from "vitest";

import type { AcceptanceCriterion } from "./api/generated";
import type { DashboardResponse, TaskDetailResponse, TaskRead } from "./api/types";
import { client } from "./api/generated/client.gen";
import { App } from "./App";
import { apiMockServer } from "./test/api-mock";
import { setViewport } from "./test/viewport";

/**
 * Running a task's acceptance checks from the task page (task-152).
 *
 * The claim these make is the one a component test cannot: that pressing the button
 * produces the right **request**, and that what comes back reaches the page without
 * anybody reloading it. The component test next to `AcceptanceChecks.tsx` proves the
 * handler is called; this proves what the handler does.
 */

const CRITERIA: Array<AcceptanceCriterion> = [
  { id: "ac-1", text: "The gate is green.", check: ["poetry", "run", "pytest", "-q"], status: "pending" },
  { id: "ac-2", text: "It reads well on a phone.", status: "pending" },
];

function task(overrides: Partial<TaskRead> = {}): TaskRead {
  return {
    schema: 2,
    id: "task-001",
    title: "A task with a check",
    created: "2026-09-22T08:00:00Z",
    updated: "2026-09-22T09:00:00Z",
    lifecycle: "active",
    ball: "agent",
    ball_reason: "work",
    ball_prompt: "Carry on.",
    display_status: "Being worked",
    priority: "high",
    category: "general",
    tags: [],
    assignment: { eligible: [] },
    spec: { summary: "Summary.", description: "Body." },
    acceptance: CRITERIA,
    log: [],
    ...overrides,
  } as TaskRead;
}

const DASHBOARD: DashboardResponse = {
  stats: { total: 1, in_progress: 1, blocked: 0, waiting_for_human: 0, awaiting_input: 0, completed: 0 },
  active_tasks: [],
  recent_updates: [],
  waiting_tasks: [],
  backlog_tasks: [],
  next_task: null,
  queue_preview: [],
  next_action: "nothing_claimable",
  broken_files: [],
  identity: { ok: true, user: "Jeff Posey", problem: null, detail: "Acting as Jeff Posey." },
};

function detail(record: TaskRead): TaskDetailResponse {
  return {
    task: record,
    parent_task: null,
    children: [],
    needs: [],
    blocks: [],
    related: [],
    child_dependency_edges: [],
    identity: { ok: true, user: "Jeff Posey", problem: null, detail: "Acting as Jeff Posey." },
  };
}

/** The record as it stands after one pass in which the check exited 1. */
function afterAFailingPass(): TaskRead {
  return task({
    acceptance: [{ ...CRITERIA[0]!, status: "failed" }, CRITERIA[1]!],
    log: [
      {
        id: 1,
        ts: "2026-09-22T09:05:00Z",
        actor: "Jeff Posey",
        type: "check_result",
        body: null,
        data: {
          chain_id: null,
          iteration: null,
          unchecked: ["ac-2"],
          results: [
            {
              id: "ac-1",
              status: "failed",
              exit_code: 1,
              duration_seconds: 3.5,
              cause: null,
              output_tail: "FAILED tests/test_loop.py::test_converges",
            },
          ],
        },
      },
    ],
  });
}

let current: TaskRead;

function handlers() {
  return [
    http.get("*/api/projects", () =>
      HttpResponse.json([
        { id: "inbox", name: "Inbox", root: "C:/projects/inbox", task_count: 1, tasks_directory: "C:/projects/inbox/tasks", default_user: "Jeff Posey" },
      ]),
    ),
    http.get("*/api/projects/inbox/tasks", () => HttpResponse.json([current])),
    http.get("*/api/projects/inbox/tasks/broken", () => HttpResponse.json([])),
    http.get("*/api/projects/inbox/tasks/task-001/detail", () => HttpResponse.json(detail(current))),
    http.get("*/api/projects/inbox/dashboard", () => HttpResponse.json(DASHBOARD)),
    http.get("*/api/projects/inbox/dispatch", () =>
      HttpResponse.json({ enabled: true, runners: [], groups: [], reason: null }),
    ),
    http.get("*/api/projects/inbox/dispatch/runs", () => HttpResponse.json([])),
    http.get("*/api/projects/inbox/dispatch/finishes/task-001", () => HttpResponse.json(null)),
    http.get("*/api/projects/inbox/revision", () => HttpResponse.json({ revision: "check-test", task_count: 1 })),
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
  current = task();
  apiMockServer.use(...handlers());
});

describe("Run checks on the task page", () => {
  it("posts to the check route with no body, and shows the result without a reload", async () => {
    let body: string | null = null;
    apiMockServer.use(
      http.post("*/api/projects/inbox/tasks/task-001/check", async ({ request }) => {
        body = await request.text();
        // The pass ran and wrote its entry, which is what a refetch will now find.
        current = afterAFailingPass();
        return HttpResponse.json({
          task_id: "task-001",
          results: afterAFailingPass().log?.[0]?.data?.results,
          unchecked: ["ac-2"],
          ok: false,
          entry_id: 1,
        });
      }),
    );
    renderApp();

    expect(await screen.findByText("Check not yet run")).toBeVisible();
    fireEvent.click(await screen.findByRole("button", { name: "Run checks" }));

    // The page goes to the result on its own. Nobody reloads, and nothing here
    // navigates: the same rendered page changes what it says.
    expect(await screen.findByText("Check failed in 3.5s — exited 1")).toBeVisible();
    expect(screen.queryByText("Check not yet run")).toBeNull();
    // And the failure's output is there to open.
    fireEvent.click(screen.getByText("Output"));
    expect(screen.getByText(/test_converges/)).toBeVisible();

    // No body, because there is no field in which a browser could name a command for
    // this machine to run. An empty string is what `fetch` sends for no body at all.
    expect(body).toBe("");
  });

  it("shows the server's own refusal rather than a paraphrase of it", async () => {
    apiMockServer.use(
      http.post("*/api/projects/inbox/tasks/task-001/check", () =>
        HttpResponse.json(
          {
            code: "disabled",
            message: "Dispatch is disabled on this machine.",
            detail: "Dispatch is disabled on this machine.",
            suggested_action: "Turn it on in the dispatch panel.",
          },
          { status: 409 },
        ),
      ),
    );
    renderApp();

    fireEvent.click(await screen.findByRole("button", { name: "Run checks" }));

    const alert = await screen.findByRole("alert");
    // Rendered by the component the Dispatch panel uses, under the gate's own reason
    // code -- so the server's sentence and its remedy both reach the page, unrewritten.
    expect(alert.getAttribute("data-refusal-reason")).toBe("disabled");
    expect(alert.textContent).toContain("Dispatch is disabled on this machine.");
    expect(alert.textContent).toContain("Turn it on in the dispatch panel.");
    // Nothing ran, so nothing moved.
    expect(screen.getByText("Check not yet run")).toBeVisible();
  });

  it("offers no button on a task whose criteria are all prose", async () => {
    current = task({ acceptance: [CRITERIA[1]!] });
    renderApp();

    expect(await screen.findByText("It reads well on a phone.")).toBeVisible();
    await waitFor(() => expect(screen.queryByRole("button", { name: "Run checks" })).toBeNull());
    expect(screen.queryByText(/^Check /)).toBeNull();
  });
});
