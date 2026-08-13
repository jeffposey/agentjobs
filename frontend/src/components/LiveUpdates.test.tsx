import { act, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { getProjectRevisionApiProjectsProjectIdRevisionGet } from "../api/generated";
import {
  FAST_RETRY_MS,
  isProjectTaskQuery,
  LiveUpdateStatus,
  NORMAL_POLL_MS,
} from "./LiveUpdates";

vi.mock("../api/generated", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api/generated")>()),
  getProjectRevisionApiProjectsProjectIdRevisionGet: vi.fn(),
}));

const getRevision = vi.mocked(getProjectRevisionApiProjectsProjectIdRevisionGet);

function response(revision: string) {
  return { data: { revision, task_count: 3 }, request: new Request("http://agentjobs.test"), response: new Response() };
}

function setup() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const invalidate = vi.spyOn(queryClient, "invalidateQueries").mockResolvedValue();
  const view = render(
    <QueryClientProvider client={queryClient}>
      <LiveUpdateStatus projectId="alpha" />
      <p>Last good task content</p>
    </QueryClientProvider>,
  );
  return { ...view, invalidate };
}

async function advance(milliseconds: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(milliseconds);
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  getRevision.mockReset();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("live project revision polling", () => {
  it("uses only the small revision request in steady state", async () => {
    getRevision.mockResolvedValue(response("rev-a"));
    const { invalidate } = setup();

    await advance(0);
    await advance(NORMAL_POLL_MS);

    expect(getRevision).toHaveBeenCalledTimes(2);
    expect(getRevision).toHaveBeenNthCalledWith(2, expect.objectContaining({
      path: { project_id: "alpha" },
      throwOnError: true,
    }));
    expect(invalidate).not.toHaveBeenCalled();
  });

  it("coalesces a bulk revision jump into one scoped query refresh", async () => {
    getRevision
      .mockResolvedValueOnce(response("rev-before"))
      .mockResolvedValueOnce(response("rev-after-bulk-write"));
    const { invalidate } = setup();

    await advance(0);
    await advance(NORMAL_POLL_MS);

    expect(invalidate).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("status")).toHaveTextContent("Task data updated just now.");
  });

  it("preserves last-good content after one miss and warns after the prompt retry also misses", async () => {
    getRevision
      .mockResolvedValueOnce(response("rev-good"))
      .mockRejectedValueOnce(new Error("first miss"))
      .mockRejectedValueOnce(new Error("second miss"));
    setup();

    await advance(0);
    await advance(NORMAL_POLL_MS);

    expect(screen.getByText("Last good task content")).toBeVisible();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();

    await advance(FAST_RETRY_MS);

    expect(screen.getByText("Last good task content")).toBeVisible();
    expect(screen.getByRole("alert")).toHaveTextContent("Live updates are paused.");
    expect(getRevision).toHaveBeenCalledTimes(3);
  });

  it("checks immediately on focus, visibility return, and online as a retry hint", async () => {
    getRevision.mockResolvedValue(response("rev-stable"));
    setup();
    await advance(0);

    window.dispatchEvent(new Event("focus"));
    await advance(0);
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
    document.dispatchEvent(new Event("visibilitychange"));
    await advance(0);
    window.dispatchEvent(new Event("online"));
    await advance(0);

    expect(getRevision).toHaveBeenCalledTimes(4);
  });

  it("cleans up its timer and event listeners", async () => {
    getRevision.mockResolvedValue(response("rev-stable"));
    const { unmount } = setup();
    await advance(0);
    unmount();

    await advance(NORMAL_POLL_MS * 2);
    window.dispatchEvent(new Event("focus"));
    await advance(0);

    expect(getRevision).toHaveBeenCalledTimes(1);
  });
});

describe("scoped query invalidation", () => {
  function query(id: string, projectId: string) {
    return { queryKey: [{ _id: id, path: { project_id: projectId } }] };
  }

  it("matches task reads for this project and nothing else", () => {
    expect(isProjectTaskQuery(query("getDashboardApiProjectsProjectIdDashboardGet", "alpha") as never, "alpha")).toBe(true);
    expect(isProjectTaskQuery(query("getTaskDetailApiProjectsProjectIdTasksTaskIdDetailGet", "alpha") as never, "alpha")).toBe(true);
    expect(isProjectTaskQuery(query("getDashboardApiProjectsProjectIdDashboardGet", "beta") as never, "alpha")).toBe(false);
    expect(isProjectTaskQuery(query("getProjectsApiProjectsGet", "alpha") as never, "alpha")).toBe(false);
    expect(isProjectTaskQuery(query("listWebhooksApiProjectsProjectIdWebhooksGet", "alpha") as never, "alpha")).toBe(false);
    expect(isProjectTaskQuery(query("getProjectRevisionApiProjectsProjectIdRevisionGet", "alpha") as never, "alpha")).toBe(false);
  });
});
