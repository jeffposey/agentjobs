import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { describe, expect, it, vi } from "vitest";

import { client } from "../api/generated/client.gen";
import { apiMockServer } from "../test/api-mock";
import { NextExplanation } from "./NextExplanation";

const EXPLANATION = {
  task: "task-120",
  band: "high",
  queue_position: 100,
  empty_bands_above: ["critical"],
  skipped: [
    { task: "task-081", position: 50, reason: "not ready (active, held by agent)" },
    { task: "task-137", position: 80, reason: "has 7 open children" },
  ],
};

function renderPanel() {
  client.setConfig({ baseUrl: "http://localhost" });
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <NextExplanation projectId="sandbox" />
    </QueryClientProvider>,
  );
}

function open() {
  const disclosure = screen.getByText("Why this one?").closest("details") as HTMLDetailsElement;
  disclosure.open = true;
  fireEvent(disclosure, new Event("toggle"));
  return disclosure;
}

describe("NextExplanation", () => {
  it("does not read the queue until somebody asks", async () => {
    const handler = vi.fn(() => HttpResponse.json(EXPLANATION));
    apiMockServer.use(http.get("*/api/projects/sandbox/tasks/next/explain", handler));
    renderPanel();

    // The dashboard polls. Fetching a walk over every open task ahead of the winner on
    // every refresh, to fill a panel nobody has expanded, is how a good idea gets
    // deleted later for being slow.
    expect(screen.getByText("Why this one?")).toBeVisible();
    expect(handler).not.toHaveBeenCalled();

    open();
    await waitFor(() => expect(handler).toHaveBeenCalledTimes(1));
  });

  it("names what stood ahead of the winner and the rule that passed each one over", async () => {
    apiMockServer.use(
      http.get("*/api/projects/sandbox/tasks/next/explain", () => HttpResponse.json(EXPLANATION)),
    );
    renderPanel();
    open();

    await screen.findByText(/has 7 open children/);
    expect(screen.getByText("task-081")).toBeVisible();
    expect(screen.getByText(/not ready \(active, held by agent\)/)).toBeVisible();
    // The band above is empty, which is a fact worth stating rather than inferring
    // from a heading that is not there.
    expect(screen.getByText(/critical/)).toBeVisible();
  });

  it("says nothing stands ahead when nothing does", async () => {
    apiMockServer.use(
      http.get("*/api/projects/sandbox/tasks/next/explain", () =>
        HttpResponse.json({ ...EXPLANATION, skipped: [], empty_bands_above: [] }),
      ),
    );
    renderPanel();
    open();

    await screen.findByText(/it is first in line and claimable/);
  });

  it("stays quiet and reloadable when the queue cannot explain itself", async () => {
    apiMockServer.use(
      http.get("*/api/projects/sandbox/tasks/next/explain", () =>
        HttpResponse.json({ detail: "the queue is broken" }, { status: 409 }),
      ),
    );
    renderPanel();
    open();

    await screen.findByText(/could not explain itself just now/);
  });
});
