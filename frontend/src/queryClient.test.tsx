import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { client } from "./api/generated/client.gen";
import { NORMAL_POLL_MS, invalidateProjectTaskQueries } from "./components/LiveUpdates";
import { CACHE_STALE_MS, createQueryClient } from "./queryClient";
import { apiMockServer } from "./test/api-mock";
import { TaskList } from "./components/TaskList";
import { useQuery } from "@tanstack/react-query";
import { listTasksApiProjectsProjectIdTasksGetOptions } from "./api/generated/@tanstack/react-query.gen";

/**
 * The caching policy, tested for the behaviour it exists to produce.
 *
 * The defect was not that data was wrong, it was that the app refetched data it
 * already had every single time a component mounted -- so a click on a task row had
 * to wait behind two refetches of the list it had just left. These tests pin the
 * property that fixed it, and the property that must not have been broken along the
 * way: an explicit invalidation still refetches.
 */

function TaskListHarness({ projectId }: { projectId: string }) {
  const tasksQuery = useQuery(
    listTasksApiProjectsProjectIdTasksGetOptions({ path: { project_id: projectId } }),
  );
  if (tasksQuery.isPending) return <p>loading</p>;
  return (
    <TaskList tasks={tasksQuery.data ?? []} brokenFiles={[]} projectId={projectId} />
  );
}

const TASK = {
  schema: 2,
  id: "task-001-cached",
  title: "Cached task",
  created: "2026-08-13T21:00:00Z",
  updated: "2026-08-13T21:00:00Z",
  lifecycle: "ready",
  ball: "agent",
  ball_reason: "available",
  archived: false,
  priority: "medium",
  category: "testing",
  tags: [],
  assignment: { eligible: [] },
  spec: { summary: "Summary", description: "Description", context: [] },
  acceptance: [],
  deliverables: [],
  dependencies: [],
  links: [],
  branches: [],
  log: [],
};

describe("the app's caching policy", () => {
  it("is derived from the revision poll interval, not picked arbitrarily", () => {
    // Pinned because the two are coupled: the poll is what keeps data fresh, so
    // changing the poll interval without revisiting this leaves the cache policy
    // stranded against a mechanism that no longer runs at that rate.
    expect(CACHE_STALE_MS).toBe(2 * NORMAL_POLL_MS);
    expect(CACHE_STALE_MS).toBeGreaterThan(0);
  });

  it("does not refetch a list it already has when the view remounts", async () => {
    client.setConfig({ baseUrl: "http://localhost" });
    let listRequests = 0;
    apiMockServer.use(
      http.get("*/api/projects/inbox/tasks", () => {
        listRequests += 1;
        return HttpResponse.json([TASK]);
      }),
    );

    const queryClient = createQueryClient();
    const view = (
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={["/p/inbox/tasks"]}>
          <Routes>
            <Route path="/p/:projectId/tasks" element={<TaskListHarness projectId="inbox" />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    );

    const first = render(view);
    await waitFor(() => expect(screen.getByText("Cached task")).toBeInTheDocument());
    expect(listRequests).toBe(1);

    // Leaving the list and coming back is the interaction that was costing a click
    // several hundred milliseconds.
    first.unmount();
    render(view);
    await waitFor(() => expect(screen.getByText("Cached task")).toBeInTheDocument());
    expect(listRequests).toBe(1);
  });

  it("still refetches when the revision poll says a task file changed", async () => {
    client.setConfig({ baseUrl: "http://localhost" });
    let listRequests = 0;
    let title = "Before the external write";
    apiMockServer.use(
      http.get("*/api/projects/inbox/tasks", () => {
        listRequests += 1;
        return HttpResponse.json([{ ...TASK, title }]);
      }),
    );

    const queryClient = createQueryClient();
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={["/p/inbox/tasks"]}>
          <Routes>
            <Route path="/p/:projectId/tasks" element={<TaskListHarness projectId="inbox" />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );
    await waitFor(() => expect(screen.getByText("Before the external write")).toBeInTheDocument());
    expect(listRequests).toBe(1);

    // What the poller does when the project revision changes. staleTime must not
    // suppress this: an explicit invalidation is the app's freshness mechanism, and
    // caching that ignored it would hide writes from the CLI, git and other agents.
    title = "After the external write";
    await invalidateProjectTaskQueries(queryClient, "inbox");

    await waitFor(() => expect(screen.getByText("After the external write")).toBeInTheDocument());
    expect(listRequests).toBe(2);
  });
});
