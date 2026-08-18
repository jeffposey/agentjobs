import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { DispatchRunView, DispatchStateView } from "../api/types";
import {
  DispatchPanel,
  DispatchSettings,
  formatElapsed,
  runStateLabel,
  runsPollInterval,
} from "./DispatchPanel";

/**
 * These assert on the words a human reads, never on the presence of an attribute.
 *
 * A test that finds `data-run-state=` passes just as happily while the page emits
 * `RunState.RUNNING`, which is the failure this repository has actually shipped. So
 * every assertion below is either visible text or the value a user's click acts on.
 */

function state(overrides: Partial<DispatchStateView> = {}): DispatchStateView {
  return {
    project_id: "sandbox",
    configured: true,
    master_enabled: true,
    sentinel_active: false,
    project_enabled: true,
    runner: "claude-session",
    posture: "supervised",
    auto_dispatch: false,
    available_runners: ["claude-session", "claude-batch"],
    can_dispatch: true,
    refusal: null,
    config_path: "C:/Users/j/.agentjobs/dispatch.yaml",
    sentinel_file: "C:/Users/j/.agentjobs/DISPATCH_DISABLED",
    ...overrides,
  };
}

function run(overrides: Partial<DispatchRunView> = {}): DispatchRunView {
  return {
    run_id: "run_abc123",
    task_id: "task-073",
    project_id: "sandbox",
    mode: "batch",
    posture: "supervised",
    status: "running",
    outcome: null,
    session_id: null,
    started_at: "2026-08-18T10:00:00Z",
    elapsed_seconds: 42,
    live: true,
    caused_by: 7,
    output_url: "/api/projects/sandbox/dispatch/runs/run_abc123/output",
    ...overrides,
  };
}

function renderPanel(props: Partial<Parameters<typeof DispatchPanel>[0]> = {}) {
  const onDispatch = vi.fn(async () => undefined);
  const onCancel = vi.fn(async (_runId: string) => undefined);
  render(
    <DispatchPanel
      state={state()}
      runs={[]}
      taskIsDispatchable
      onDispatch={onDispatch}
      onCancel={onCancel}
      {...props}
    />,
  );
  return { onDispatch, onCancel };
}

describe("the Dispatch action", () => {
  it("is a separate control from Approve, and says what it costs", () => {
    renderPanel();

    const button = screen.getByRole("button", { name: /dispatch/i });
    expect(button).toHaveTextContent("start an agent now");
    // The panel says it is not approval, in the copy a human reads rather than only in
    // a comment. Approve lives in a different panel entirely (ReviewPanel).
    expect(screen.getByText(/this is not approval/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /approve/i })).toBeNull();
  });

  it("names the runner and posture the click would use", () => {
    renderPanel();

    const panel = screen.getByRole("region", { name: "Dispatch" });
    expect(panel).toHaveTextContent("claude-session");
    expect(panel).toHaveTextContent("supervised");
  });

  it("starts a run when clicked", async () => {
    const { onDispatch } = renderPanel();

    fireEvent.click(screen.getByRole("button", { name: /dispatch/i }));

    await waitFor(() => expect(onDispatch).toHaveBeenCalledTimes(1));
  });

  it("is not offered at all when the task's ball is not with an agent", () => {
    renderPanel({ taskIsDispatchable: false });

    expect(screen.queryByRole("region", { name: "Dispatch" })).toBeNull();
  });

  it("still shows past runs on a task nobody can dispatch any more", () => {
    renderPanel({ taskIsDispatchable: false, runs: [run({ live: false, status: "finished", outcome: "completed" })] });

    expect(screen.getByRole("region", { name: "Dispatch" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /dispatch/i })).toBeNull();
    expect(screen.getByText("Completed")).toBeInTheDocument();
  });
});

describe("refusals", () => {
  it("renders the specific gate that is closed, not 'dispatch failed'", () => {
    renderPanel({
      state: state({
        can_dispatch: false,
        project_enabled: false,
        refusal: {
          reason: "project_not_enabled",
          message: "Project 'sandbox' is not enabled for dispatch.",
        },
      }),
    });

    expect(screen.getByRole("alert")).toHaveTextContent(
      "Project 'sandbox' is not enabled for dispatch.",
    );
    expect(screen.getByRole("alert")).toHaveTextContent(/not enabled for dispatch\. Turn it on/i);
    expect(screen.queryByRole("button", { name: /dispatch/i })).toBeNull();
  });

  it("renders the human-clocked rule in the words that explain why retrying will not help", () => {
    renderPanel({
      dispatchRefusal: {
        reason: "not_human_clocked",
        message: "The newest log entry was written by claude.",
        suggestedAction: null,
      },
    });

    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("The newest log entry was written by claude.");
    expect(alert).toHaveTextContent(/not configurable/i);
  });

  it("prefers the server's own suggested action when it sends one", () => {
    renderPanel({
      dispatchRefusal: {
        reason: "sentinel",
        message: "Dispatch is disabled by C:/Users/j/.agentjobs/DISPATCH_DISABLED.",
        suggestedAction: "Delete C:/Users/j/.agentjobs/DISPATCH_DISABLED to re-enable dispatch.",
      },
    });

    expect(screen.getByRole("alert")).toHaveTextContent(
      "Delete C:/Users/j/.agentjobs/DISPATCH_DISABLED to re-enable dispatch.",
    );
  });

  it("says something useful even when the server could not be reached at all", () => {
    renderPanel({
      dispatchRefusal: {
        reason: "unreachable",
        message: "AgentJobs could not be reached to start a run.",
        suggestedAction: "Check that the server is still running, then try again.",
      },
    });

    expect(screen.getByRole("alert")).toHaveTextContent("could not be reached");
  });
});

describe("the run list", () => {
  it("shows a live run's elapsed time, a cancel button, and a link to its output", async () => {
    const { onCancel } = renderPanel({ runs: [run({ elapsed_seconds: 95 })] });

    const row = screen.getByRole("listitem");
    expect(within(row).getByText("Running")).toBeInTheDocument();
    expect(row).toHaveTextContent("Running for 1m 35s");
    expect(within(row).getByRole("link", { name: /view output/i })).toHaveAttribute(
      "href",
      "/api/projects/sandbox/dispatch/runs/run_abc123/output",
    );

    fireEvent.click(within(row).getByRole("button", { name: /cancel run/i }));
    await waitFor(() => expect(onCancel).toHaveBeenCalledWith("run_abc123"));
  });

  it("shows a finished run's outcome and offers no cancel", () => {
    renderPanel({
      runs: [run({ live: false, status: "cancelled", outcome: "cancelled", elapsed_seconds: 8 })],
    });

    const row = screen.getByRole("listitem");
    expect(within(row).getByText("Cancelled")).toBeInTheDocument();
    expect(row).toHaveTextContent("Ran for 8s");
    expect(within(row).queryByRole("button", { name: /cancel run/i })).toBeNull();
    expect(within(row).getByRole("link", { name: /view output/i })).toBeInTheDocument();
  });

  it("never shows a raw enum spelling for an outcome", () => {
    renderPanel({
      runs: [
        run({ live: false, status: "finished", outcome: "finished_without_handoff" }),
      ],
    });

    expect(screen.getByText("Stopped without saying what it needs")).toBeInTheDocument();
    expect(screen.queryByText(/finished_without_handoff/)).toBeNull();
  });
});

describe("run polling", () => {
  it("polls while anything is live and stops entirely when nothing is", () => {
    expect(runsPollInterval([run({ live: true })])).toBe(2_000);
    expect(runsPollInterval([run({ live: false })])).toBe(false);
    expect(runsPollInterval([])).toBe(false);
  });
});

describe("elapsed formatting", () => {
  it("reads as a duration at every scale, and says so when it is unknown", () => {
    expect(formatElapsed(9)).toBe("9s");
    expect(formatElapsed(95)).toBe("1m 35s");
    expect(formatElapsed(3_725)).toBe("1h 02m");
    expect(formatElapsed(null)).toBe("unknown");
  });
});

describe("run state labels", () => {
  it("calls a live run running whatever its recorded outcome says", () => {
    expect(runStateLabel(run({ live: true, outcome: "failed" }))).toBe("Running");
  });
});

function renderSettings(value: DispatchStateView | null, busy = false, error: string | null = null) {
  const onEnable = vi.fn(async (_runner: string | null) => undefined);
  const onDisable = vi.fn(async () => undefined);
  render(
    <DispatchSettings
      state={value}
      busy={busy}
      error={error}
      onEnable={onEnable}
      onDisable={onDisable}
    />,
  );
  return { onEnable, onDisable };
}

describe("the project toggle", () => {
  it("offers disable with no ceremony while dispatch is on", async () => {
    const { onDisable } = renderSettings(state());

    const button = screen.getByRole("button", { name: /disable dispatch/i });
    fireEvent.click(button);

    // One click, no confirmation dialog, no reason demanded.
    await waitFor(() => expect(onDisable).toHaveBeenCalledTimes(1));
  });

  it("enables against a runner chosen from the ones this machine defines", async () => {
    const { onEnable } = renderSettings(state({ project_enabled: false, runner: null }));

    const select = screen.getByLabelText("Runner");
    expect(
      [...select.querySelectorAll("option")].map((option) => option.textContent),
    ).toEqual(["claude-session", "claude-batch"]);

    fireEvent.change(select, { target: { value: "claude-batch" } });
    fireEvent.click(screen.getByRole("button", { name: /enable dispatch/i }));

    await waitFor(() => expect(onEnable).toHaveBeenCalledWith("claude-batch"));
  });

  it("offers no way to type a runner command", () => {
    renderSettings(state({ project_enabled: false, runner: null }));

    // The runner control is a closed list, not a text field. A free-text box here
    // would let the browser name a command, which is the whole thing the gates exist
    // to prevent.
    expect(screen.getByLabelText("Runner").tagName).toBe("SELECT");
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(screen.getByText(/never from this page/i)).toBeInTheDocument();
  });

  it("cannot be enabled at all when this machine defines no runners", () => {
    renderSettings(state({ project_enabled: false, runner: null, available_runners: [] }));

    expect(screen.getByRole("button", { name: /enable dispatch/i })).toBeDisabled();
    expect(screen.getByLabelText("Runner")).toBeDisabled();
  });

  it("reports each gate separately, so the reader knows which thing to fix", () => {
    renderSettings(
      state({
        master_enabled: false,
        can_dispatch: false,
        refusal: { reason: "disabled", message: "Dispatch is switched off." },
      }),
    );

    const machineGate = screen.getByText("Machine-wide switch").closest("div");
    expect(machineGate).toHaveTextContent("Closed");
    const projectGate = screen.getByText("This project").closest("div");
    expect(projectGate).toHaveTextContent("Open");
    expect(screen.getByRole("alert")).toHaveTextContent(/set 'enabled: true'/i);
  });

  it("names the sentinel file when the kill switch is what is stopping runs", () => {
    renderSettings(
      state({
        sentinel_active: true,
        can_dispatch: false,
        refusal: {
          reason: "sentinel",
          message: "Dispatch is disabled by the sentinel file.",
        },
      }),
    );

    expect(screen.getByText(/DISPATCH_DISABLED exists/)).toBeInTheDocument();
  });

  it("says it is still reading rather than rendering an empty page", () => {
    renderSettings(null);

    expect(screen.getByText(/reading this machine's dispatch configuration/i)).toBeInTheDocument();
  });
});
