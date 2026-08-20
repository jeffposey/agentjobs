import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { ReviewIdentity } from "../api/generated";
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

/** A resolvable signed-in human, which is the ordinary case on a configured project. */
function identity(overrides: Partial<ReviewIdentity> = {}): ReviewIdentity {
  return { ok: true, user: "Jeff Posey", problem: null, detail: "", ...overrides };
}

function renderPanel(props: Partial<Parameters<typeof DispatchPanel>[0]> = {}) {
  // Resolves `true`: the default is a dispatch that started. A refusal is `false`, and
  // the tests that need one say so, because the difference decides whether the human's
  // typed brief survives.
  const onDispatch = vi.fn(async (_note?: string) => true);
  const onCancel = vi.fn(async (_runId: string) => undefined);
  render(
    <DispatchPanel
      state={state()}
      runs={[]}
      taskIsDispatchable
      identity={identity()}
      recordCanBrief
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

describe("one click (task-188)", () => {
  it("dispatches with no text on a task whose record can brief an agent", async () => {
    // The 97% case. Before this, the human had to know to write a note first, on a task
    // whose only failing was that an agent had filed it.
    const { onDispatch } = renderPanel({ recordCanBrief: true });

    expect(screen.queryByRole("textbox")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /dispatch/i }));

    await waitFor(() => expect(onDispatch).toHaveBeenCalledWith());
  });

  it("names the person the run will be authorised by, before the click", () => {
    renderPanel();

    expect(screen.getByRole("region", { name: "Dispatch" })).toHaveTextContent(
      /authorised by\s*Jeff Posey/i,
    );
  });

  it("asks for text only when the record cannot brief an agent", async () => {
    const { onDispatch } = renderPanel({ recordCanBrief: false });

    const box = screen.getByRole("textbox", { name: /say what the agent should do/i });
    fireEvent.change(box, { target: { value: "Rip out the old poller." } });
    fireEvent.click(screen.getByRole("button", { name: /dispatch/i }));

    await waitFor(() => expect(onDispatch).toHaveBeenCalledWith("Rip out the old poller."));
  });

  it("will not dispatch an empty brief when it has asked for one", () => {
    const { onDispatch } = renderPanel({ recordCanBrief: false });

    expect(screen.getByRole("button", { name: /dispatch/i })).toBeDisabled();
    expect(onDispatch).not.toHaveBeenCalled();
  });

  it("keeps the typed brief when the dispatch is refused", async () => {
    // The defect Jeff found by clicking it: he typed a brief, the sandbox's only run
    // slot was busy, and the textarea came back empty. This is the one path in the
    // feature where a human has written something that exists nowhere else -- it has
    // not been saved to the task -- so a refusal that clears it costs the sentence
    // rather than a click, and after task-172 that sentence may have been dictated.
    // The handler resolves on a refusal (it renders the reason itself), so the panel
    // is told by the resolved value rather than by the promise settling.
    const onDispatch = vi.fn(async (_note?: string) => false);
    render(
      <DispatchPanel
        state={state()}
        runs={[]}
        taskIsDispatchable
        identity={identity()}
        recordCanBrief={false}
        onDispatch={onDispatch}
        onCancel={vi.fn()}
      />,
    );

    const box = screen.getByRole("textbox", { name: /say what the agent should do/i });
    fireEvent.change(box, { target: { value: "Port the widget to v2." } });
    fireEvent.click(screen.getByRole("button", { name: /dispatch/i }));

    await waitFor(() => expect(onDispatch).toHaveBeenCalledWith("Port the widget to v2."));
    expect(box).toHaveValue("Port the widget to v2.");
  });

  it("clears the brief once a run has actually started", async () => {
    // The other half, and the reason the field is cleared at all: a brief that survived
    // a successful dispatch would be retyped into the next one by anyone who did not
    // notice, and re-submitted as a second authorising entry.
    const onDispatch = vi.fn(async (_note?: string) => true);
    render(
      <DispatchPanel
        state={state()}
        runs={[]}
        taskIsDispatchable
        identity={identity()}
        recordCanBrief={false}
        onDispatch={onDispatch}
        onCancel={vi.fn()}
      />,
    );

    const box = screen.getByRole("textbox", { name: /say what the agent should do/i });
    fireEvent.change(box, { target: { value: "Port the widget to v2." } });
    fireEvent.click(screen.getByRole("button", { name: /dispatch/i }));

    await waitFor(() => expect(box).toHaveValue(""));
  });

  it("opens the box when the server says the record is insufficient, even if the page thought otherwise", () => {
    // The server is the authority on sufficiency. Honouring its answer means a drift
    // between the two checks costs one round trip rather than showing a button that
    // can only ever refuse.
    renderPanel({
      recordCanBrief: true,
      dispatchRefusal: {
        reason: "insufficient_record",
        message: "task-107 has no spec.description.",
        suggestedAction: "Say what the agent should do.",
      },
    });

    expect(screen.getByRole("textbox", { name: /say what the agent should do/i })).toBeInTheDocument();
  });

  it("refuses to offer the button at all when nobody is signed in", () => {
    // Disabled, not pressable-into-a-refusal, and never signed with a config default:
    // an entry attributed to nobody looks like evidence and is not.
    renderPanel({
      identity: {
        ok: false,
        user: null,
        problem: "unconfigured",
        detail: "No human actor is configured.",
      },
    });

    expect(screen.queryByRole("button", { name: /dispatch/i })).toBeNull();
    const note = screen.getByRole("status");
    expect(note).toHaveTextContent(/nobody is signed in/i);
    expect(note).toHaveTextContent("No human actor is configured.");
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

    // A gate that was already closed is status, not an alert: nobody pressed anything.
    expect(screen.getByRole("status")).toHaveTextContent(
      "Project 'sandbox' is not enabled for dispatch.",
    );
    expect(screen.getByRole("status")).toHaveTextContent(/not enabled for dispatch\. Turn it on/i);
    expect(screen.queryByRole("button", { name: /dispatch/i })).toBeNull();
  });

  it("says nothing at all on a machine where dispatch was never configured", () => {
    renderPanel({
      state: state({
        configured: false,
        master_enabled: false,
        project_enabled: false,
        can_dispatch: false,
        available_runners: [],
        refusal: { reason: "not_configured", message: "Dispatch is not configured." },
      }),
    });

    expect(screen.queryByRole("region", { name: "Dispatch" })).toBeNull();
  });

  it("explains a human-clocked refusal as the identity problem it now is", () => {
    // Since task-188 the button names the person clicking and the server writes their
    // authorising entry, so this refusal no longer means "your newest entry was an
    // agent's" -- it means the page had nobody to name. The remedy is therefore
    // configuration, and the copy that used to say "not configurable" would now be
    // pointing the reader away from the one thing that fixes it.
    renderPanel({
      dispatchRefusal: {
        reason: "not_human_clocked",
        message: "The newest log entry was written by claude.",
        suggestedAction: null,
      },
    });

    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("The newest log entry was written by claude.");
    expect(alert).toHaveTextContent(/cannot tell who is clicking/i);
    expect(alert).toHaveTextContent(/actors:/i);
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

  it("points a task with nothing on its log at the control that writes one", () => {
    renderPanel({
      dispatchRefusal: {
        reason: "no_causing_entry",
        message: "task-107 has no log entries, so there is nothing a dispatch could be caused by.",
        // What the CLI and MCP are told. Correct for them and useless here: it names an
        // act, not a control, and this reader is looking at a page.
        suggestedAction: "Write the note or handoff that authorises this run first.",
      },
    });

    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("has no log entries");
    expect(alert).toHaveTextContent(/use “Add a note” below/i);
    expect(alert).not.toHaveTextContent("Write the note or handoff that authorises this run first.");
  });

  it("points an agent-filed task at the same control rather than at a concept", () => {
    renderPanel({
      dispatchRefusal: {
        reason: "not_human_clocked",
        message: "Log entry 1 (transition) was written by 'claude', an agent.",
        // What the CLI and MCP are told: correct for them, and it names an act rather
        // than a control, so the page overrides it. Task-185's rule, unchanged.
        suggestedAction: "Act on the task yourself, then dispatch. This rule is not configurable.",
      },
    });

    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent(/use “Add a note” below/i);
    expect(alert).not.toHaveTextContent("Act on the task yourself, then dispatch.");
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
    expect(screen.getByRole("status")).toHaveTextContent(/set 'enabled: true'/i);
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

  it("says whether auto-dispatch is armed, and offers no control for it", () => {
    renderSettings(state({ auto_dispatch: true }));

    expect(screen.getByText(/starts an agent immediately/i)).toBeInTheDocument();
    // No switch here on purpose: this is the one setting that turns a click into an
    // unattended run, so it moves only by editing the machine-local file.
    expect(screen.queryByRole("checkbox")).toBeNull();
    expect(screen.queryByRole("button", { name: /auto-dispatch/i })).toBeNull();
  });

  it("says auto-dispatch is off when it is, in the same place", () => {
    renderSettings(state({ auto_dispatch: false }));

    expect(screen.getByText(/records the approval and starts nothing/i)).toBeInTheDocument();
  });

  it("says it is still reading rather than rendering an empty page", () => {
    renderSettings(null);

    expect(screen.getByText(/reading this machine's dispatch configuration/i)).toBeInTheDocument();
  });
});
