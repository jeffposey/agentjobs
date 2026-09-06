import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it } from "vitest";

import type { DashboardResponse, TaskDetailResponse, TaskRead } from "./api/types";
import { client } from "./api/generated/client.gen";
import { App } from "./App";
import { apiMockServer } from "./test/api-mock";
import { setViewport } from "./test/viewport";

/**
 * The shape of the Tasks surface: two regions, one route, and what each viewport gets.
 *
 * These render the whole `App` rather than a component, because the claim is about the
 * *composition* -- which routes are siblings, which region a banner sits outside of,
 * and what a deep link resolves to. A component test cannot make any of those, and the
 * defect this epic exists to remove (opening a task unmounts the list) lived entirely
 * in the route table.
 *
 * What they are not evidence for: independent scrolling and the page not scrolling as
 * one unit. jsdom does not lay out, so `overflow-y` means nothing to it. That is
 * asserted in `e2e/tasks-shell.spec.ts` against a measured browser.
 */

const DESKTOP = { width: 1280, height: 800 };
/** iPhone 14/15 CSS pixels, which is where this app is read over Tailscale. */
const PHONE = { width: 390, height: 844 };

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
  active_tasks: [task("task-001", { lifecycle: "active", display_status: "In flight" })],
  recent_updates: [],
  waiting_tasks: [],
  backlog_tasks: [],
  next_task: null,
  queue_preview: [],
  next_action: "nothing_claimable",
  broken_files: [],
  identity: { ok: true, user: "jeff", problem: null, detail: "Acting as jeff." },
};

function handlers(options: { broken?: boolean; queueBroken?: boolean } = {}) {
  return [
    http.get("*/api/projects", () =>
      HttpResponse.json([
        { id: "inbox", name: "Inbox", root: "C:/projects/inbox", task_count: TASKS.length, tasks_directory: "C:/projects/inbox/tasks", default_user: "jeff" },
      ]),
    ),
    http.get("*/api/projects/inbox/tasks", () => HttpResponse.json(TASKS)),
    http.get("*/api/projects/inbox/tasks/broken", () =>
      HttpResponse.json(
        options.broken
          ? [{ task_id: "task-unreadable", path: "C:/projects/inbox/tasks/task-unreadable.yaml", filename: "task-unreadable.yaml", reason: "schema: Input should be 2" }]
          : [],
      ),
    ),
    http.get("*/api/projects/inbox/queue", () =>
      HttpResponse.json({
        bands: [],
        problems: options.queueBroken
          ? [{ kind: "duplicate", band: "high", tasks: ["task-001", "task-002"], position: 100, message: "band 'high' position 100 is claimed by task-001, task-002" }]
          : [],
        repair_command: "agentjobs queue repair",
      }),
    ),
    http.get("*/api/projects/inbox/tasks/task-001/detail", () => HttpResponse.json(DETAIL)),
    http.get("*/api/projects/inbox/dashboard", () => HttpResponse.json(DASHBOARD)),
    http.get("*/api/projects/inbox/dispatch", () => HttpResponse.json({ enabled: false, runners: [], groups: [], reason: "Dispatch is off in this fixture." })),
    http.get("*/api/projects/inbox/dispatch/runs", () => HttpResponse.json([])),
    http.get("*/api/projects/inbox/dispatch/finishes/task-001", () => HttpResponse.json(null)),
    http.get("*/api/projects/inbox/revision", () => HttpResponse.json({ revision: "shell-test", task_count: TASKS.length })),
    http.get("*/api/projects/inbox/attention", () => HttpResponse.json({ blocking: 0 })),
  ];
}

function renderApp(entry: string) {
  const queryClient = new QueryClient({
    defaultOptions: { mutations: { retry: false }, queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[entry]}>
        <App />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const listRegion = () => screen.queryByRole("complementary", { name: "Task list" });
const detailRegion = () => screen.queryByRole("region", { name: "Task detail" });

beforeEach(() => {
  client.setConfig({ baseUrl: "http://localhost" });
  apiMockServer.use(...handlers());
});

describe("the Tasks surface at a landscape viewport", () => {
  beforeEach(() => setViewport(DESKTOP.width, DESKTOP.height));

  it("renders the list and the selected task's record at the same time", async () => {
    renderApp("/p/inbox/tasks/task-001");

    // The deep link resolves to the record, and the list is beside it rather than
    // replaced by it -- which is the whole defect this epic exists to remove.
    const detail = await screen.findByRole("region", { name: "Task detail" });
    // The heading, not the text: the list renders the same title in the row that links
    // to it, so a bare text match would pass with the record still loading.
    expect(await within(detail).findByRole("heading", { name: "Title of task-001" })).toBeVisible();

    const list = listRegion();
    expect(list).not.toBeNull();
    expect(within(list as HTMLElement).getByRole("region", { name: "Tasks" })).toBeVisible();
    expect(within(list as HTMLElement).getByText("task-002")).toBeVisible();
  });

  it("fills the detail region with a deliberate prompt when nothing is selected", async () => {
    renderApp("/p/inbox/tasks");

    const empty = await screen.findByRole("region", { name: "No task selected" });
    expect(empty).toBeVisible();
    expect(empty).toHaveTextContent(/Choose a task in the list/);
    expect(listRegion()).not.toBeNull();
  });

  it("keeps the other surfaces full width, with no regions and an unchanged nav", async () => {
    renderApp("/p/inbox");

    await screen.findByRole("navigation", { name: "Primary navigation" });
    expect(listRegion()).toBeNull();
    expect(detailRegion()).toBeNull();
    // The header nav is the global navigation and this epic does not touch it.
    const nav = screen.getByRole("navigation", { name: "Primary navigation" });
    for (const label of ["Dashboard", "Tasks", "Create", "Dispatch"]) {
      expect(within(nav).getByText(label, { exact: true })).toBeInTheDocument();
    }
  });

  it("lands on the Tasks surface, list included, when a task is opened from the Dashboard", async () => {
    renderApp("/p/inbox");

    // The real path: the Dashboard's own link, clicked, rather than a URL asserted to
    // be the one it would have produced.
    const link = await screen.findByRole("link", { name: /task-001/ });
    fireEvent.click(link);

    const detail = await screen.findByRole("region", { name: "Task detail" });
    expect(await within(detail).findByRole("heading", { name: "Title of task-001" })).toBeVisible();
    // Not a detail-only page: the list is beside it, with the task it opened in it.
    expect(listRegion()).not.toBeNull();
  });

  it("keeps the broken-file banner outside both regions", async () => {
    apiMockServer.use(...handlers({ broken: true }));
    renderApp("/p/inbox/tasks/task-001");

    const banner = await screen.findByRole("region", { name: "Unreadable task files" });
    expect(banner).toHaveTextContent("task-unreadable.yaml — schema: Input should be 2");
    // Inside either scrollport it could be scrolled past, and would be invisible from
    // the other region entirely.
    await waitFor(() => expect(listRegion()).not.toBeNull());
    expect(listRegion()?.contains(banner)).toBe(false);
    expect(detailRegion()?.contains(banner)).toBe(false);
  });

  it("keeps the broken-queue banner outside both regions", async () => {
    apiMockServer.use(...handlers({ queueBroken: true }));
    renderApp("/p/inbox/tasks/task-001");

    const banner = await screen.findByRole("alert", { name: "Queue is broken" });
    expect(banner).toHaveTextContent("band 'high' position 100 is claimed by task-001, task-002");
    expect(banner).toHaveTextContent("agentjobs queue repair");
    await waitFor(() => expect(listRegion()).not.toBeNull());
    expect(listRegion()?.contains(banner)).toBe(false);
    expect(detailRegion()?.contains(banner)).toBe(false);
  });
});

describe("the Tasks surface at a phone viewport", () => {
  beforeEach(() => setViewport(PHONE.width, PHONE.height));

  it("shows the record alone when a task is open, exactly as it did before", async () => {
    renderApp("/p/inbox/tasks/task-001");

    expect(await screen.findByText("Title of task-001")).toBeVisible();
    // No regions at all: the stacked shell is one thing at a time, which is what
    // task-093 established for the phone and what this epic must not regress.
    expect(listRegion()).toBeNull();
    expect(detailRegion()).toBeNull();
    expect(screen.queryByRole("region", { name: "Tasks" })).toBeNull();
  });

  it("shows the list alone when nothing is open, with no empty region beside it", async () => {
    renderApp("/p/inbox/tasks");

    expect(await screen.findByRole("region", { name: "Tasks" })).toBeVisible();
    expect(screen.queryByRole("region", { name: "No task selected" })).toBeNull();
    expect(detailRegion()).toBeNull();
  });
});
