import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import type { LiveRunView, LiveRunsView, MachineHolderView } from "../api/types";
import { NavStatus } from "./NavStatus";

/**
 * task-588: the header's one readout -- slots used, waiting on you, being worked.
 *
 * Assertions are on the rendered numbers and the accessible name, which are what a
 * person reads and what a screen reader says. That it fits a phone is measured in a
 * browser, in e2e/attention-badge.spec.ts.
 */

function run(overrides: Partial<LiveRunView> = {}): LiveRunView {
  return {
    run_id: "run_a",
    task_id: "task-001",
    task_title: "Teach the queue to count",
    project_id: "demo",
    project_name: "Demo",
    mode: "session",
    session: true,
    posture: "auto",
    status: "running",
    health: "working",
    started_at: "2026-09-24T01:00:00Z",
    elapsed_seconds: 90,
    task_url: "/p/demo/tasks/task-001",
    output_url: "/api/projects/demo/dispatch/runs/run_a/output",
    holds_slot: true,
    ...overrides,
  };
}

function finish(): MachineHolderView {
  return {
    kind: "finish",
    lock_name: "task-002",
    task_id: "task-002",
    project_id: "demo",
    project_name: "Demo",
    finish_id: "fin_abc",
    pid: 1234,
    started_at: "2026-09-24T01:00:00Z",
    elapsed_seconds: 30,
    detail: "gate",
    task_url: "/p/demo/tasks/task-002",
  };
}

function runs(overrides: Partial<LiveRunsView> = {}): LiveRunsView {
  return {
    occupied: 0,
    max_concurrent_runs: 3,
    dispatch_configured: true,
    runs: [],
    holders: [],
    generated_at: "2026-09-24T01:01:30Z",
    ...overrides,
  };
}

function renderStatus(props: Partial<Parameters<typeof NavStatus>[0]> = {}) {
  return render(
    <MemoryRouter initialEntries={["/p/demo/tasks"]}>
      <NavStatus runs={null} waiting={null} projectId="demo" {...props} />
    </MemoryRouter>,
  );
}

const status = () => screen.getByTestId("nav-status");
const count = (id: string) => screen.getByTestId(id).getAttribute("data-count");

describe("NavStatus", () => {
  it("shows slots used, waiting on you and being worked", () => {
    renderStatus({
      runs: runs({ occupied: 2, runs: [run(), run({ run_id: "run_b" })] }),
      waiting: 1,
    });
    expect(screen.getByTestId("nav-status-slots")).toHaveTextContent("2/3");
    expect(count("nav-status-waiting")).toBe("1");
    expect(count("nav-status-working")).toBe("2");
  });

  it("spells all three out for a screen reader", () => {
    renderStatus({
      runs: runs({ occupied: 2, runs: [run(), run({ run_id: "run_b" })] }),
      waiting: 1,
    });
    expect(
      screen.getByRole("link", {
        name: "2 of 3 slots busy · 1 waiting on you · 2 being worked",
      }),
    ).toBe(status());
    expect(status()).toHaveAttribute("title", status().getAttribute("aria-label"));
  });

  it("reads over the ceiling honestly rather than clamping", () => {
    renderStatus({ runs: runs({ occupied: 3, max_concurrent_runs: 2 }), waiting: 0 });
    expect(screen.getByTestId("nav-status-slots")).toHaveTextContent("3/2");
    expect(status()).toHaveAccessibleName(/3 of 2 slots busy · 1 over the ceiling/);
  });

  it("leaves the slots out on a machine with no dispatch configured", () => {
    // No denominator exists, and the running count is already the green part.
    renderStatus({
      runs: runs({ dispatch_configured: false, occupied: 1, runs: [run()] }),
      waiting: 0,
    });
    expect(screen.queryByTestId("nav-status-slots")).toBeNull();
    expect(count("nav-status-working")).toBe("1");
  });

  it("draws a zero as a zero, not as nothing", () => {
    // Nothing waiting and nothing running is information; a part that vanished would
    // look the same as one that had not loaded, and would change the pill's width.
    renderStatus({ runs: runs(), waiting: 0 });
    expect(screen.getByTestId("nav-status-waiting")).toHaveTextContent("0");
    expect(screen.getByTestId("nav-status-working")).toHaveTextContent("0");
    expect(status()).toHaveAccessibleName("0 of 3 slots busy · 0 waiting on you · 0 being worked");
  });

  it("reads zero before either answer arrives", () => {
    renderStatus();
    expect(count("nav-status-waiting")).toBe("0");
    expect(count("nav-status-working")).toBe("0");
    expect(screen.queryByTestId("nav-status-slots")).toBeNull();
  });

  it("counts a finish in progress as being worked", () => {
    // runningCount's rule (task-352), carried over from the old green badge.
    renderStatus({ runs: runs({ holders: [finish()] }), waiting: 0 });
    expect(count("nav-status-working")).toBe("1");
  });

  it("caps the waiting number so the pill stops changing width, and still says the real one", () => {
    renderStatus({ runs: runs(), waiting: 14 });
    expect(screen.getByTestId("nav-status-waiting")).toHaveTextContent("9+");
    expect(status()).toHaveAccessibleName(/14 waiting on you/);
  });

  it("leads to the dashboard of the project the bar is scoped to", () => {
    renderStatus({ runs: runs(), waiting: 2 });
    expect(status()).toHaveAttribute("href", "/p/demo");
  });

  it("acknowledges the attention episode when followed (task-422)", () => {
    const onAcknowledge = vi.fn();
    renderStatus({ runs: runs(), waiting: 2, onAcknowledge });
    fireEvent.click(status());
    expect(onAcknowledge).toHaveBeenCalledTimes(1);
  });
});
