import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { describe, expect, it } from "vitest";

import { client } from "../api/generated/client.gen";
import { apiMockServer } from "../test/api-mock";
import { EmergencyStop, StoppedBanner } from "./EmergencyStop";

/**
 * task-573: the header's emergency stop.
 *
 * Assertions are on what the server was asked and on what a person reads. The control's
 * place in the header, and that it fits a phone, are measured in a browser in
 * e2e/emergency-stop.spec.ts.
 */

const STOPPED = {
  stopped: true,
  note: "written by Jeff Posey from the web UI at 2026-09-24T22:00:00+00:00",
  sentinel_file: "~/.agentjobs/DISPATCH_DISABLED",
};

function renderStop() {
  client.setConfig({ baseUrl: "http://localhost" });
  const queryClient = new QueryClient({
    defaultOptions: { mutations: { retry: false }, queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <EmergencyStop />
      <StoppedBanner />
    </QueryClientProvider>,
  );
}

describe("EmergencyStop", () => {
  it("asks once before stopping, and a cancel sends nothing", async () => {
    let pressed = 0;
    apiMockServer.use(
      http.post("*/api/runs/emergency-stop", () => {
        pressed += 1;
        return HttpResponse.json({ ...STOPPED, items: [] });
      }),
    );
    renderStop();

    fireEvent.click(screen.getByRole("button", { name: "Emergency stop" }));
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText("Stop everything?")).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));

    expect(screen.queryByRole("dialog")).toBeNull();
    expect(pressed).toBe(0);
  });

  it("lists everything the stop acted on, and becomes the stopped state", async () => {
    let stopped = false;
    apiMockServer.use(
      http.get("*/api/runs/emergency-stop", () =>
        HttpResponse.json(stopped ? STOPPED : { ...STOPPED, stopped: false, note: "" }),
      ),
      http.post("*/api/runs/emergency-stop", () => {
        stopped = true;
        return HttpResponse.json({
          ...STOPPED,
          items: [
            { id: "run_a1", kind: "run", stopped: true, detail: "session stopped" },
            {
              id: "walk_b2",
              kind: "walk",
              stopped: true,
              detail: "epic walk of task-212 ended",
            },
            { id: "run_c3", kind: "run", stopped: false, detail: "stop not confirmed" },
          ],
        });
      }),
    );
    renderStop();

    fireEvent.click(await screen.findByRole("button", { name: "Emergency stop" }));
    fireEvent.click(screen.getByRole("button", { name: "Stop everything" }));

    const results = await screen.findByTestId("emergency-stop-results");
    expect(within(results).getAllByRole("listitem")).toHaveLength(3);
    expect(results).toHaveTextContent("epic walk of task-212 ended");
    expect(results).toHaveTextContent("not confirmed");
    fireEvent.click(screen.getByRole("button", { name: "Close" }));

    const trigger = await screen.findByRole("button", { name: "Dispatch stopped — resume" });
    expect(trigger).toHaveAttribute("data-stopped", "true");
    expect(screen.getByTestId("stopped-banner")).toHaveTextContent("Dispatch stopped.");
  });

  it("offers Resume while stopped, and resuming clears the state", async () => {
    let stopped = true;
    let resumed = 0;
    apiMockServer.use(
      http.get("*/api/runs/emergency-stop", () =>
        HttpResponse.json(stopped ? STOPPED : { ...STOPPED, stopped: false, note: "" }),
      ),
      http.post("*/api/runs/emergency-stop/resume", () => {
        resumed += 1;
        stopped = false;
        return HttpResponse.json({ ...STOPPED, stopped: false, note: "" });
      }),
    );
    renderStop();

    fireEvent.click(await screen.findByRole("button", { name: "Dispatch stopped — resume" }));
    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveTextContent("written by Jeff Posey from the web UI");
    fireEvent.click(within(dialog).getByRole("button", { name: "Resume dispatch" }));

    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(resumed).toBe(1);
    expect(await screen.findByRole("button", { name: "Emergency stop" })).toHaveAttribute(
      "data-stopped",
      "false",
    );
    expect(screen.queryByTestId("stopped-banner")).toBeNull();
  });

  it("draws no strip while dispatch is running", async () => {
    renderStop();
    expect(await screen.findByRole("button", { name: "Emergency stop" })).toBeInTheDocument();
    expect(screen.queryByTestId("stopped-banner")).toBeNull();
  });

  it("offers Resume from the strip as well as from the trigger", async () => {
    apiMockServer.use(http.get("*/api/runs/emergency-stop", () => HttpResponse.json(STOPPED)));
    renderStop();

    fireEvent.click(await screen.findByRole("button", { name: "Resume…" }));
    expect(screen.getByRole("dialog")).toHaveTextContent("Dispatch is stopped");
    expect(screen.getByRole("button", { name: "Resume dispatch" })).toBeInTheDocument();
  });

  it("says why when a resume is refused", async () => {
    apiMockServer.use(
      http.get("*/api/runs/emergency-stop", () => HttpResponse.json(STOPPED)),
      http.post("*/api/runs/emergency-stop/resume", () =>
        HttpResponse.json(
          { code: "capability_denied", message: "This caller may not resume dispatch." },
          { status: 403 },
        ),
      ),
    );
    renderStop();

    fireEvent.click(await screen.findByRole("button", { name: "Dispatch stopped — resume" }));
    fireEvent.click(screen.getByRole("button", { name: "Resume dispatch" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "This caller may not resume dispatch.",
    );
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });
});
