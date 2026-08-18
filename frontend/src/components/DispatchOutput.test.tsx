import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { describe, expect, it } from "vitest";

import type { DispatchRunView } from "../api/types";
import { client } from "../api/generated/client.gen";
import { apiMockServer } from "../test/api-mock";
import {
  DispatchRunOutput,
  TAIL_POLL_MS,
  emptyOutputNote,
  sourceNote,
  tailPollInterval,
} from "./DispatchOutput";

/**
 * These are about the requirement Jeff added to task-157: output has to be readable
 * *while* a run is going, in one collapsible place that still holds it afterwards.
 */

function run(overrides: Partial<DispatchRunView> = {}): DispatchRunView {
  return {
    run_id: "run_abc123",
    task_id: "task-001",
    project_id: "sandbox",
    mode: "session",
    posture: "autonomous",
    status: "running",
    outcome: null,
    session_id: "b55b35ad",
    started_at: "2026-08-18T10:00:00+00:00",
    elapsed_seconds: 42,
    live: true,
    caused_by: 3,
    output_url: "/api/projects/sandbox/dispatch/runs/run_abc123/output",
    ...overrides,
  };
}

function tail(overrides: Record<string, unknown> = {}) {
  return {
    run_id: "run_abc123",
    live: true,
    source: "session-transcript",
    lines: 40,
    text: "Reading the task record\nMaking a change",
    updated_at: "2026-08-18T10:01:00+00:00",
    ...overrides,
  };
}

function renderOutput(view: DispatchRunView) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <DispatchRunOutput run={view} />
    </QueryClientProvider>,
  );
}

describe("DispatchRunOutput", () => {
  it("shows a live run's output on the task page without anyone clicking through", async () => {
    client.setConfig({ baseUrl: "http://localhost" });
    apiMockServer.use(
      http.get("*/api/projects/sandbox/dispatch/runs/run_abc123/tail", () =>
        HttpResponse.json(tail()),
      ),
    );

    renderOutput(run());

    expect(screen.getByRole("button", { name: /Output/ })).toHaveAttribute(
      "aria-expanded",
      "true",
    );
    expect(await screen.findByText(/Making a change/)).toBeInTheDocument();
  });

  it("holds the finished output in the same section, collapsed until asked for", async () => {
    client.setConfig({ baseUrl: "http://localhost" });
    apiMockServer.use(
      http.get("*/api/projects/sandbox/dispatch/runs/run_abc123/tail", () =>
        HttpResponse.json(tail({ live: false, text: "exiting 0" })),
      ),
    );

    renderOutput(run({ live: false, status: "finished", outcome: "completed" }));

    const toggle = screen.getByRole("button", { name: /Output/ });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText(/exiting 0/)).not.toBeInTheDocument();

    fireEvent.click(toggle);

    expect(await screen.findByText(/exiting 0/)).toBeInTheDocument();
  });

  it("says why there is nothing yet rather than showing an empty box", async () => {
    client.setConfig({ baseUrl: "http://localhost" });
    apiMockServer.use(
      http.get("*/api/projects/sandbox/dispatch/runs/run_abc123/tail", () =>
        HttpResponse.json(tail({ source: "none", text: "" })),
      ),
    );

    renderOutput(run());

    expect(await screen.findByText(/Nothing captured yet/)).toBeInTheDocument();
  });

  it("does not report a live run as dead when its output cannot be read", async () => {
    client.setConfig({ baseUrl: "http://localhost" });
    apiMockServer.use(
      http.get("*/api/projects/sandbox/dispatch/runs/run_abc123/tail", () =>
        HttpResponse.json({ detail: "boom" }, { status: 500 }),
      ),
    );

    renderOutput(run());

    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent(/could not be read just now/),
    );
  });

  it("asks for nothing at all while it is collapsed", async () => {
    client.setConfig({ baseUrl: "http://localhost" });
    let requests = 0;
    apiMockServer.use(
      http.get("*/api/projects/sandbox/dispatch/runs/run_abc123/tail", () => {
        requests += 1;
        return HttpResponse.json(tail({ live: false }));
      }),
    );

    renderOutput(run({ live: false }));

    await waitFor(() => expect(screen.getByRole("button", { name: /Output/ })).toBeVisible());
    expect(requests).toBe(0);
  });
});

describe("the tail's clock", () => {
  it("never runs faster than the poller that produces what it reads", () => {
    // The text comes from a file the session poller writes by shelling out to the
    // runner. A browser polling faster would read the same bytes back sooner; a browser
    // fetching it *itself* would spawn a process per watcher, which is the thing this
    // interval exists to keep impossible.
    expect(TAIL_POLL_MS).toBeGreaterThanOrEqual(10_000);
    expect(tailPollInterval(true)).toBe(TAIL_POLL_MS);
    expect(tailPollInterval(false)).toBe(false);
  });

  it("distinguishes 'nothing yet' from 'nothing at all'", () => {
    expect(emptyOutputNote(true, "none")).toMatch(/Nothing captured yet/);
    expect(emptyOutputNote(false, "none")).toMatch(/captured no output/);
    expect(emptyOutputNote(true, "session-transcript")).toBe("");
  });

  it("names where the text came from, because the two sources differ", () => {
    expect(sourceNote("session-transcript")).toMatch(/session's own transcript/);
    expect(sourceNote("captured-output")).toMatch(/stdout and stderr/);
  });
});
