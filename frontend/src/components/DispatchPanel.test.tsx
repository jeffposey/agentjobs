import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { ReviewIdentity } from "../api/generated";
import type { DispatchRunView, DispatchStateView, QueuedDispatchState } from "../api/types";
import {
  DispatchPanel,
  DispatchSettings,
  formatElapsed,
  runStateLabel,
  runsPollInterval,
  type DispatchEnableTarget,
  type DispatchOptions,
  type PullArmChoice,
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
    merge_mode: "review",
    merge_mode_phrases: {
      review: "Hands off for your review",
      automerge: "Merges itself on a green gate",
    },
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
    merge_mode: "review",
    merge_mode_phrase: "Hands off for your review",
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
  const onDispatch = vi.fn(async (_options?: DispatchOptions) => true);
  const onCancel = vi.fn(async (_runId: string) => undefined);
  render(
    <DispatchPanel
      state={state()}
      runs={[]}
      taskIsDispatchable
      heldByAgent={null}
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

  it("names the runner and merge mode the click would use", () => {
    renderPanel();

    const panel = screen.getByRole("region", { name: "Dispatch" });
    expect(panel).toHaveTextContent("claude-session");
    expect(panel).toHaveTextContent(/merge mode\s*review:\s*hands off for your review/);
  });

  it("starts a run when clicked", async () => {
    const { onDispatch } = renderPanel();

    fireEvent.click(screen.getByRole("button", { name: /dispatch/i }));

    await waitFor(() => expect(onDispatch).toHaveBeenCalledTimes(1));
  });

  it("is unavailable while a finish is working on this task's branch", () => {
    // The refusal exists server-side either way -- the finish holds the task's run
    // lock. What this adds is that the page says so *before* the click, because the
    // reason it is unavailable is a thing the reader started themselves by pressing
    // Approve thirty seconds ago (task-321).
    const { onDispatch } = renderPanel({ finishLive: true });

    expect(screen.getByRole("button", { name: /dispatch/i })).toBeDisabled();
    expect(
      document.querySelector('[data-refusal-reason="finish_in_progress"]'),
    ).toHaveTextContent(/cannot be started/i);
    fireEvent.click(screen.getByRole("button", { name: /dispatch/i }));
    expect(onDispatch).not.toHaveBeenCalled();
  });

  it("describes a live finish as status, never as an alert", () => {
    // Polled every two seconds while one runs, so announcing it as an alert would make
    // a screen reader interrupt on every poll. Same rule `RefusalNote` follows.
    renderPanel({ finishLive: true });

    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByRole("status")).toHaveAttribute(
      "data-refusal-reason",
      "finish_in_progress",
    );
  });

  it("is unavailable while an epic walk supervises this task, and says so", () => {
    // task-591: the click would start a second walk that the first turns away as
    // `already_supervised`. The walk's own claim must not read as an unseen agent.
    const { onDispatch } = renderPanel({
      walkOpen: true,
      heldByAgent: { owner: "claude", since: "2026-09-25T15:40:56Z" },
    });

    expect(screen.getByRole("button", { name: /dispatch/i })).toBeDisabled();
    expect(screen.getByRole("status")).toHaveAttribute(
      "data-refusal-reason",
      "already_supervised",
    );
    expect(screen.getByRole("status")).toHaveTextContent(
      "An epic walk is supervising this task and starts its children itself, so there is nothing to dispatch.",
    );
    expect(document.querySelector('[data-refusal-reason="task_being_worked"]')).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /dispatch/i }));
    expect(onDispatch).not.toHaveBeenCalled();
  });

  it("is offered as before when no walk is open", () => {
    renderPanel({ walkOpen: false });

    expect(screen.getByRole("button", { name: /dispatch/i })).toBeEnabled();
    expect(document.querySelector('[data-refusal-reason="already_supervised"]')).toBeNull();
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

    await waitFor(() => expect(onDispatch).toHaveBeenCalledWith({}));
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

    await waitFor(() =>
      expect(onDispatch).toHaveBeenCalledWith({ note: "Rip out the old poller." }),
    );
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
    const onDispatch = vi.fn(async (_options?: DispatchOptions) => false);
    render(
      <DispatchPanel
        state={state()}
        runs={[]}
        taskIsDispatchable
        heldByAgent={null}
        identity={identity()}
        recordCanBrief={false}
        onDispatch={onDispatch}
        onCancel={vi.fn()}
      />,
    );

    const box = screen.getByRole("textbox", { name: /say what the agent should do/i });
    fireEvent.change(box, { target: { value: "Port the widget to v2." } });
    fireEvent.click(screen.getByRole("button", { name: /dispatch/i }));

    await waitFor(() =>
      expect(onDispatch).toHaveBeenCalledWith({ note: "Port the widget to v2." }),
    );
    expect(box).toHaveValue("Port the widget to v2.");
  });

  it("clears the brief once a run has actually started", async () => {
    // The other half, and the reason the field is cleared at all: a brief that survived
    // a successful dispatch would be retyped into the next one by anyone who did not
    // notice, and re-submitted as a second authorising entry.
    const onDispatch = vi.fn(async (_options?: DispatchOptions) => true);
    render(
      <DispatchPanel
        state={state()}
        runs={[]}
        taskIsDispatchable
        heldByAgent={null}
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

  it("names the runs holding the machine's slots, not just how many", () => {
    // The whole of the complaint that produced this: the panel's own run list shows
    // only *this* task's runs, so a run occupying the machine is by definition one this
    // page cannot show. "2 run(s) already active" is therefore a dead end -- a reader is
    // told to cancel something and given nowhere to go. The server's sentence names the
    // run and the task each is working, and this asserts that it survives to the screen.
    //
    // Since task-461 it survives into the three-way prompt rather than into a refusal
    // note, which is a change of container and not of the requirement: the names are
    // exactly what a person choosing between Queue and Dispatch now is deciding on.
    renderPanel({
      dispatchRefusal: {
        reason: "concurrency_limit",
        message:
          "This machine allows 2 concurrent run(s) and 2 are active: " +
          "run_aa11 on agentjobs/task-150 (running), run_bb22 on agentjobs/task-151 (starting). " +
          "Send this dispatch again with `if_full: queue` to have it start on its own " +
          "when a slot frees, cancel one of those runs, or wait for one to finish.",
        suggestedAction:
          "Send the dispatch again with if_full=queue to have it start when a slot " +
          "frees, cancel one of the runs named above, or raise "
          + "limits.max_concurrent_runs in ~/.agentjobs/dispatch.yaml.",
      },
    });

    const prompt = screen.getByTestId("dispatch-full-prompt");
    expect(prompt).toHaveTextContent("run_aa11 on agentjobs/task-150 (running)");
    expect(prompt).toHaveTextContent("run_bb22 on agentjobs/task-151 (starting)");
    // The ceiling is a number now, so nothing on this surface may call it "the only slot".
    expect(prompt).not.toHaveTextContent(/only slot/i);
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
  const onEnable = vi.fn(async (_target: DispatchEnableTarget) => undefined);
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

    await waitFor(() => expect(onEnable).toHaveBeenCalledWith({ runner: "claude-batch" }));
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

/**
 * Runner groups, in the browser (task-184).
 *
 * A project pointed at a `group:` names no `runner:` of its own, so every one of these
 * asserts the sentence a human reads rather than the field it came from -- reading only
 * `state.runner` is exactly how a fully configured project came to render "no runner
 * chosen" beside an open gate.
 */
function grouped(overrides: Partial<DispatchStateView> = {}): DispatchStateView {
  return state({
    runner: null,
    group: "default",
    available_groups: ["big", "default"],
    resolved_runner: "claude-opus-5",
    resolved_group: "default",
    resolved_from: "project",
    ...overrides,
  });
}

describe("choosing a runner group for one dispatch", () => {
  it("names the group and the member it resolves to, on a project that names no runner", () => {
    renderPanel({ state: grouped() });

    const panel = screen.getByRole("region", { name: "Dispatch" });
    expect(panel).toHaveTextContent(/Agent\s*claude-opus-5\s*from\s*Default/);
    expect(panel).toHaveTextContent(/authorised by\s*Jeff Posey/i);
  });

  it("offers groups and specific agents in one unambiguous choice", () => {
    renderPanel({ state: grouped() });

    const select = screen.getByLabelText("Run with");
    expect([...select.querySelectorAll("option")].map((option) => option.textContent)).toEqual([
      "Project default",
      "Big",
      "Default",
      "claude-session",
      "claude-batch",
    ]);
    // Nothing pre-picked: a value the human did not choose is never sent as one.
    expect(select).toHaveValue("");
    expect(screen.getByRole("group", { name: "Automatic groups" })).toBeInTheDocument();
    expect(screen.getByRole("group", { name: "Specific agents" })).toBeInTheDocument();
    expect(screen.queryByLabelText("Runner")).toBeNull();
    expect(screen.queryByLabelText("Group")).toBeNull();
  });

  it("sends the group the human picked, and only then", async () => {
    const { onDispatch } = renderPanel({ state: grouped() });

    fireEvent.click(screen.getByRole("button", { name: /dispatch/i }));
    await waitFor(() => expect(onDispatch).toHaveBeenLastCalledWith({}));

    fireEvent.change(screen.getByLabelText("Run with"), { target: { value: "group:big" } });
    fireEvent.click(screen.getByRole("button", { name: /dispatch/i }));

    await waitFor(() => expect(onDispatch).toHaveBeenLastCalledWith({ group: "big" }));
  });

  it("carries the group alongside a brief on the task that needed one", async () => {
    const { onDispatch } = renderPanel({ state: grouped(), recordCanBrief: false });

    fireEvent.change(screen.getByLabelText("Run with"), { target: { value: "group:big" } });
    fireEvent.change(screen.getByRole("textbox", { name: /say what the agent should do/i }), {
      target: { value: "Audit the guard chain." },
    });
    fireEvent.click(screen.getByRole("button", { name: /dispatch/i }));

    await waitFor(() =>
      expect(onDispatch).toHaveBeenCalledWith({ group: "big", note: "Audit the guard chain." }),
    );
  });

  it("stops naming the project's member once a different group is chosen for this run", () => {
    // The browser holds no member list, so it says which group will choose rather than
    // guessing which member wins. Leaving 'claude-opus-5' on screen beside group 'big'
    // would be the one sentence on this panel that is reliably wrong.
    renderPanel({ state: grouped() });

    fireEvent.change(screen.getByLabelText("Run with"), { target: { value: "group:big" } });

    const panel = screen.getByRole("region", { name: "Dispatch" });
    expect(panel).toHaveTextContent(/Automatic group\s*Big/);
    // The pulldown still spells out the project's default in its first option, which is
    // the whole point of that option -- what must not survive is the *claim* that
    // claude-opus-5 is what this click will run.
    expect(panel).not.toHaveTextContent(/Agent\s*claude-opus-5/);
  });

  it("uses the same single choice on a machine that defines no groups", () => {
    renderPanel();

    expect(screen.getByLabelText("Run with")).toBeInTheDocument();
    expect(screen.queryByRole("group", { name: "Automatic groups" })).toBeNull();
    expect(screen.getByRole("region", { name: "Dispatch" })).toHaveTextContent(
      /Agent\s*claude-session, merge mode\s*review: hands off for your review, authorised by\s*Jeff Posey/,
    );
  });
});

describe("choosing a specific runner for one dispatch", () => {
  it("still offers an explicit choice when exactly one runner is configured", () => {
    renderPanel({
      state: state({
        available_runners: ["codex-sol"],
        runner_labels: { "codex-sol": "ChatGPT · GPT-5.6 Sol" },
      }),
    });

    expect(screen.getByLabelText("Run with")).toHaveTextContent("ChatGPT · GPT-5.6 Sol");
  });

  it("offers every machine-local runner and sends the chosen one", async () => {
    const { onDispatch } = renderPanel();

    const select = screen.getByLabelText("Run with");
    expect([...select.querySelectorAll("option")].map((option) => option.textContent)).toEqual([
      "Project default",
      "claude-session",
      "claude-batch",
    ]);

    fireEvent.change(select, { target: { value: "runner:claude-batch" } });
    fireEvent.click(screen.getByRole("button", { name: /dispatch/i }));

    await waitFor(() => expect(onDispatch).toHaveBeenLastCalledWith({ runner: "claude-batch" }));
    expect(screen.getByRole("region", { name: "Dispatch" })).toHaveTextContent(
      /Agent\s*claude-batch/,
    );
  });

  it("shows model names while submitting stable runner ids", async () => {
    const { onDispatch } = renderPanel({
      state: grouped({
        available_runners: ["claude-fable-5-1", "codex-astra", "codex-sol"],
        runner_labels: {
          "claude-fable-5-1": "Claude Fable 5.1",
          "codex-astra": "ChatGPT · GPT-6 Astra",
          "codex-sol": "ChatGPT · GPT-5.6 Sol",
        },
      }),
    });

    const select = screen.getByLabelText("Run with");
    expect([...select.querySelectorAll("option")].map((option) => option.textContent)).toEqual([
      "Project default",
      "Big",
      "Default",
      "Claude Fable 5.1",
      "ChatGPT · GPT-6 Astra",
      "ChatGPT · GPT-5.6 Sol",
    ]);

    fireEvent.change(select, { target: { value: "runner:codex-sol" } });
    fireEvent.click(screen.getByRole("button", { name: /dispatch/i }));

    await waitFor(() => expect(onDispatch).toHaveBeenLastCalledWith({ runner: "codex-sol" }));
    expect(screen.getByRole("region", { name: "Dispatch" })).toHaveTextContent(
      /Agent\s*ChatGPT · GPT-5.6 Sol/,
    );
  });
});

/**
 * A project that allows automerge, so there is genuinely a choice to make.
 *
 * `merge_mode_phrases` is present because the server always sends it -- it is the one
 * table of display copy, not configuration. A fixture that omitted it would be testing a
 * state the API cannot produce.
 */
function withMergeModes(overrides: Partial<DispatchStateView> = {}): DispatchStateView {
  return state({
    merge_mode: "review",
    allow_automerge: true,
    offerable_merge_modes: ["review", "automerge"],
    finish_enabled: true,
    ...overrides,
  });
}

describe("choosing a merge mode for one dispatch (task-307, task-602)", () => {
  it("offers what the server permits, each saying what it does in the server's words", () => {
    renderPanel({ state: withMergeModes() });

    const select = screen.getByLabelText("Merge mode");
    expect([...select.querySelectorAll("option")].map((option) => option.textContent)).toEqual([
      "Project default (review)",
      "Review — hands off for your review",
      "Automerge — merges itself on a green gate",
    ]);
    // Nothing pre-picked, so a mode the human did not choose is never sent as one.
    expect(select).toHaveValue("");
  });

  it("renders whatever phrase the server sends rather than a copy of its own", () => {
    // task-309's constraint: the browser keeps no table of this copy.
    renderPanel({
      state: withMergeModes({
        merge_mode_phrases: { review: "Waits for you", automerge: "Lands on green" },
      }),
    });

    const options = [...screen.getByLabelText("Merge mode").querySelectorAll("option")].map(
      (option) => option.textContent,
    );
    expect(options).toContain("Review — waits for you");
    expect(options).toContain("Automerge — lands on green");
  });

  it("offers no chooser where the project does not allow automerge", () => {
    // The refusal exists server-side either way (`automerge_not_allowed`). With only
    // `review` offered there is nothing to choose, and a pulldown saying so is furniture.
    renderPanel({
      state: withMergeModes({ allow_automerge: false, offerable_merge_modes: ["review"] }),
    });

    expect(screen.queryByLabelText("Merge mode")).toBeNull();
    expect(screen.getByRole("region", { name: "Dispatch" })).toHaveTextContent(
      /merge mode\s*review:\s*hands off for your review/,
    );
  });

  it("sends the merge mode the human picked, and only then", async () => {
    const { onDispatch } = renderPanel({ state: withMergeModes() });

    fireEvent.click(screen.getByRole("button", { name: /dispatch/i }));
    await waitFor(() => expect(onDispatch).toHaveBeenLastCalledWith({}));

    fireEvent.change(screen.getByLabelText("Merge mode"), { target: { value: "automerge" } });
    fireEvent.click(screen.getByRole("button", { name: /dispatch/i }));

    await waitFor(() =>
      expect(onDispatch).toHaveBeenLastCalledWith({ merge_mode: "automerge" }),
    );
  });

  it("carries the merge mode alongside a group and a brief", async () => {
    const { onDispatch } = renderPanel({
      state: withMergeModes({ available_groups: ["big", "default"] }),
      recordCanBrief: false,
    });

    fireEvent.change(screen.getByLabelText("Run with"), { target: { value: "group:big" } });
    fireEvent.change(screen.getByLabelText("Merge mode"), { target: { value: "automerge" } });
    fireEvent.change(screen.getByRole("textbox", { name: /say what the agent should do/i }), {
      target: { value: "Audit the guard chain." },
    });
    fireEvent.click(screen.getByRole("button", { name: /dispatch/i }));

    await waitFor(() =>
      expect(onDispatch).toHaveBeenCalledWith({
        group: "big",
        merge_mode: "automerge",
        note: "Audit the guard chain.",
      }),
    );
  });

  it("says what the chosen mode will do, not what the project's default would have", () => {
    renderPanel({ state: withMergeModes() });

    const panel = screen.getByRole("region", { name: "Dispatch" });
    expect(panel).toHaveTextContent(/merge mode\s*review:\s*hands off for your review/);

    fireEvent.change(screen.getByLabelText("Merge mode"), { target: { value: "automerge" } });

    expect(panel).toHaveTextContent(/merge mode\s*automerge:\s*merges itself on a green gate/);
  });

  it("offers automerge disabled, and says why, where the project has no scripted finish", () => {
    // An automerge runs through `agentjobs finish --automerge-release`, and a machine
    // without it has no sanctioned mechanism for one.
    renderPanel({ state: withMergeModes({ finish_enabled: false }) });

    const automerge = [...screen.getByLabelText("Merge mode").querySelectorAll("option")].find(
      (option) => (option as HTMLOptionElement).value === "automerge",
    );
    expect(automerge?.textContent).toBe("Automerge — needs finish.enabled on this project");
    expect(automerge).toBeDisabled();
  });

  it("leaves review alone when the finish is off", () => {
    renderPanel({ state: withMergeModes({ finish_enabled: false }) });

    const options = [...screen.getByLabelText("Merge mode").querySelectorAll("option")];
    expect(options.filter((option) => option.hasAttribute("disabled"))).toHaveLength(1);
  });

  it("names pushing only where the project permits it", () => {
    // Per project and never the merge mode's (task-021), and false everywhere today --
    // so a sentence repeating the universal default on every task would be noise.
    renderPanel({ state: withMergeModes() });
    expect(screen.getByRole("region", { name: "Dispatch" })).not.toHaveTextContent(/and pushes/);
  });

  it("says so where pushing is permitted, because that is the unrecoverable half", () => {
    renderPanel({ state: withMergeModes({ push: true }) });

    expect(screen.getByRole("region", { name: "Dispatch" })).toHaveTextContent(/, and pushes,/);
  });
});

describe("the project gate tile, with groups", () => {
  it("names the group and the member that would run, never 'no runner chosen'", () => {
    renderSettings(grouped());

    const tile = screen.getByText("This project").closest("div");
    expect(tile).toHaveTextContent("Open");
    expect(tile).toHaveTextContent("group: default → claude-opus-5");
    expect(tile).not.toHaveTextContent("no runner chosen");
  });

  it("says when the group came from the machine rather than the project", () => {
    renderSettings(
      grouped({
        group: null,
        default_group: "big",
        resolved_group: "big",
        resolved_from: "machine",
      }),
    );

    expect(screen.getByText("This project").closest("div")).toHaveTextContent(
      "group: big (machine default) → claude-opus-5",
    );
  });

  it("still names the group when a shut gate means nothing resolved", () => {
    renderSettings(
      grouped({
        project_enabled: false,
        resolved_runner: null,
        resolved_group: null,
        resolved_from: null,
        can_dispatch: false,
        refusal: { reason: "project_not_enabled", message: "Not enabled." },
      }),
    );

    const tile = screen.getByText("This project").closest("div");
    expect(tile).toHaveTextContent("Closed");
    expect(tile).toHaveTextContent("group: default");
  });

  it("still says 'no runner chosen' when nothing at all is configured", () => {
    // sc-4's other half: the old sentence is still the right one, and still appears.
    renderSettings(state({ runner: null, project_enabled: false }));

    expect(screen.getByText("This project").closest("div")).toHaveTextContent("no runner chosen");
  });

  it("still names a plain runner on a machine with no groups", () => {
    renderSettings(state());

    expect(screen.getByText("This project").closest("div")).toHaveTextContent(
      "runner: claude-session",
    );
  });
});

describe("enabling a project against a group", () => {
  it("offers groups and runners in one list, preselecting what the project already uses", () => {
    renderSettings(grouped({ project_enabled: false }));

    const select = screen.getByLabelText("Runner or group");
    expect([...select.querySelectorAll("option")].map((option) => option.textContent)).toEqual([
      "group: Big",
      "group: Default",
      "agent: claude-session",
      "agent: claude-batch",
    ]);
    // Until task-184 this fell through to the first runner, so pressing Enable on a
    // grouped project offered to change something the config layer then silently
    // declined to change -- established against a throwaway home before anything here
    // was touched.
    expect(select).toHaveValue("group:default");
  });

  it("points the project at a group, and never at a group and a runner at once", async () => {
    const { onEnable } = renderSettings(grouped({ project_enabled: false }));

    fireEvent.change(screen.getByLabelText("Runner or group"), { target: { value: "group:big" } });
    fireEvent.click(screen.getByRole("button", { name: /enable dispatch/i }));

    await waitFor(() => expect(onEnable).toHaveBeenCalledWith({ group: "big" }));
  });

  it("still points it at a plain runner when that is what was picked", async () => {
    const { onEnable } = renderSettings(grouped({ project_enabled: false }));

    fireEvent.change(screen.getByLabelText("Runner or group"), {
      target: { value: "claude-batch" },
    });
    fireEvent.click(screen.getByRole("button", { name: /enable dispatch/i }));

    await waitFor(() => expect(onEnable).toHaveBeenCalledWith({ runner: "claude-batch" }));
  });

  it("keeps the plain 'Runner' control on a machine that defines no groups", () => {
    // sc-4 again, on the settings page: bare names, the old label, no mention of groups.
    renderSettings(state({ project_enabled: false, runner: null }));

    expect(screen.queryByLabelText("Runner or group")).toBeNull();
    expect(screen.getByLabelText("Runner")).toBeInTheDocument();
    expect(screen.getByText(/never from this page/i).textContent).toMatch(
      /^Runners are defined by hand/,
    );
  });

  it("says groups are hand-written too, on a machine that has them", () => {
    renderSettings(grouped({ project_enabled: false }));

    expect(screen.getByText(/never from this page/i).textContent).toMatch(
      /^Runners and runner groups are defined by hand/,
    );
  });
});

describe("a task that something is already working (task-354)", () => {
  it("withholds the Dispatch button while a session is working the task", () => {
    // The defect: a task claimed and being worked from a chat window showed a button
    // offering to start a second agent on the same task and the same repository. The
    // server refuses that with `live_run_exists`; the page now says so before the click.
    renderPanel({ runs: [run({ mode: "interactive", live: true })] });

    expect(screen.queryByRole("button", { name: /^▶ Dispatch/ })).toBeNull();
    const note = screen.getByRole("status");
    expect(note).toHaveAttribute("data-refusal-reason", "live_run_exists");
    expect(note).toHaveTextContent("A session is working this task right now");
    expect(note).toHaveTextContent("will not stop it");
  });

  it("says a dispatched run differently, because that one can be cancelled", () => {
    renderPanel({ runs: [run({ mode: "session", live: true })] });

    expect(screen.queryByRole("button", { name: /^▶ Dispatch/ })).toBeNull();
    const note = screen.getByRole("status");
    expect(note).toHaveTextContent("An agent is already running on this task");
    expect(note).toHaveTextContent("Cancel it there");
  });

  it("offers the button again once the run is over", () => {
    renderPanel({ runs: [run({ live: false, status: "finished", outcome: "completed" })] });

    expect(screen.getByRole("button", { name: /^▶ Dispatch/ })).toBeInTheDocument();
    expect(screen.queryByText(/already working/i)).toBeNull();
  });
});

/**
 * The other half of that defect, and the dangerous one (task-179).
 *
 * The block above reads the ledger, so it only ever sees agents AgentJobs started. An
 * agent from the spawn-session skill, or a person in a terminal, writes a claim on the
 * task record and nothing else — no run row, no lock — so to every test above it looks
 * exactly like a task nobody is working.
 */
describe("a task held by an agent AgentJobs never started (task-179)", () => {
  // What `heldByAgent` returns for an unsuperseded claim: the owner put the ball on
  // itself and nothing has moved it since. A handover — a person approving, the finisher
  // escalating — resolves to null there and never reaches this panel, which is why every
  // case below is about what to draw rather than about whether to draw it.
  const held = { owner: "claude", since: "2026-08-19T14:05:00Z" };

  it("withholds the Dispatch button and names who is holding the task", () => {
    renderPanel({ heldByAgent: held, runs: [] });

    expect(screen.queryByRole("button", { name: /^▶ Dispatch/ })).toBeNull();
    const note = screen.getByRole("status");
    expect(note).toHaveAttribute("data-refusal-reason", "task_being_worked");
    expect(note).toHaveTextContent("claude");
    expect(note).toHaveTextContent(/claimed this task/i);
    expect(note).toHaveTextContent(/nothing has moved the ball since/i);
  });

  it("says AgentJobs cannot see or stop it, and what ends it", () => {
    // The sentence that stops the reader hunting for a Cancel button. A dispatched run
    // has one, right below; this has nothing, and saying so is the difference between a
    // withheld control that explains itself and one that just looks broken.
    renderPanel({ heldByAgent: held, runs: [] });

    const note = screen.getByRole("status");
    expect(note).toHaveTextContent(/did not start it/i);
    expect(note).toHaveTextContent(/hands the task off, releases it, or closes it/i);
    expect(note).toHaveTextContent(/release the task/i);
  });

  it("says when it was claimed, so the reader can judge whether it is still alive", () => {
    renderPanel({ heldByAgent: held, runs: [] });

    // The rendered time, not the ISO string the record carries: a reader deciding
    // whether an agent is stuck reads a clock, and asserting on the input would pass
    // against a page showing the raw timestamp.
    expect(screen.getByRole("status")).toHaveTextContent(
      new Date(held.since).toLocaleString(),
    );
  });

  it("offers the button when a finished run accounts for the claim", () => {
    // The distinction the server makes, made identically here: a finished run is
    // AgentJobs saying it started an agent at this task and watched it end, which is
    // the one thing that explains a claim left behind by a process that is gone. Same
    // `heldByAgent` as every test above — only the run list differs.
    renderPanel({
      heldByAgent: held,
      runs: [run({ live: false, status: "finished", outcome: "completed" })],
    });

    expect(screen.getByRole("button", { name: /^▶ Dispatch/ })).toBeInTheDocument();
    expect(screen.queryByText(/did not start it/i)).toBeNull();
  });

  it("leaves a live run to the gate that knows about runs", () => {
    // Both conditions true at once, which is the ordinary state of any dispatched run:
    // the record says an agent is working it *and* the ledger has the run. One box, and
    // it is the one that can offer a Cancel.
    renderPanel({ heldByAgent: held, runs: [run({ mode: "session", live: true })] });

    expect(screen.queryByRole("button", { name: /^▶ Dispatch/ })).toBeNull();
    const note = screen.getByRole("status");
    expect(note).toHaveAttribute("data-refusal-reason", "live_run_exists");
  });

  it("offers the button on a task nobody has claimed", () => {
    // The state almost every dispatch is made from. `agent/available` and `agent/work`
    // were indistinguishable to the expression this replaced; they must not become
    // indistinguishable again in the direction that withholds the button from everyone.
    renderPanel({ heldByAgent: null, runs: [] });

    expect(screen.getByRole("button", { name: /^▶ Dispatch/ })).toBeInTheDocument();
  });
});

/**
 * The three-way prompt a full machine gets (task-461).
 *
 * Every assertion here is about what the click *sends* — or, for Cancel, about it
 * sending nothing. A test that only found the buttons would pass against a prompt whose
 * Queue button dispatched at full price, which is the one failure that costs money.
 */
describe("what the panel does when the machine is full", () => {
  const holders = "run_aa11 on agentjobs/task-150 (running)";
  const full = state({
    machine_full: true,
    machine_occupied: 1,
    machine_ceiling: 1,
    slot_holders: holders,
  });
  const refusal = {
    reason: "concurrency_limit",
    message: `This machine allows 1 concurrent run(s) and 1 is active: ${holders}.`,
  };

  it("asks instead of sending, and names the runs holding the slots", () => {
    const { onDispatch } = renderPanel({ state: full });

    fireEvent.click(screen.getByRole("button", { name: /^▶ Dispatch/ }));

    const prompt = screen.getByTestId("dispatch-full-prompt");
    expect(prompt).toHaveTextContent("Every slot on this machine is taken — 1 of 1");
    expect(prompt).toHaveTextContent(holders);
    expect(within(prompt).getByTestId("dispatch-queue-it")).toBeVisible();
    expect(within(prompt).getByTestId("dispatch-over-ceiling")).toBeVisible();
    expect(within(prompt).getByTestId("dispatch-full-cancel")).toBeVisible();
    // The click that opened it spent nothing. This is the whole difference between a
    // prompt and a refusal: the refusal is what you get for having already asked.
    expect(onDispatch).not.toHaveBeenCalled();
  });

  it("sends if_full=queue for Queue, and nothing else it was not told to", () => {
    const { onDispatch } = renderPanel({ state: full });
    fireEvent.click(screen.getByRole("button", { name: /^▶ Dispatch/ }));

    fireEvent.click(screen.getByTestId("dispatch-queue-it"));

    expect(onDispatch).toHaveBeenCalledWith({ if_full: "queue" });
  });

  it("sends over_ceiling for Dispatch now, and never both", () => {
    const { onDispatch } = renderPanel({ state: full });
    fireEvent.click(screen.getByRole("button", { name: /^▶ Dispatch/ }));

    fireEvent.click(screen.getByTestId("dispatch-over-ceiling"));

    expect(onDispatch).toHaveBeenCalledWith({ over_ceiling: true });
    // The server refuses the pair with a 400, so a panel that sent both would turn a
    // deliberate choice into a validation error.
    expect(onDispatch.mock.calls[0]?.[0]).not.toHaveProperty("if_full");
  });

  it("sends nothing at all for Cancel, and goes back to resting", () => {
    const { onDispatch } = renderPanel({ state: full });
    fireEvent.click(screen.getByRole("button", { name: /^▶ Dispatch/ }));

    fireEvent.click(screen.getByTestId("dispatch-full-cancel"));

    expect(onDispatch).not.toHaveBeenCalled();
    expect(screen.queryByTestId("dispatch-full-prompt")).not.toBeInTheDocument();
    // Still pressable. Cancel answers this question, not every future one.
    expect(screen.getByRole("button", { name: /^▶ Dispatch/ })).toBeInTheDocument();
  });

  it("treats Escape as Cancel", () => {
    const { onDispatch } = renderPanel({ state: full });
    fireEvent.click(screen.getByRole("button", { name: /^▶ Dispatch/ }));

    fireEvent.keyDown(screen.getByTestId("dispatch-full-prompt"), { key: "Escape" });

    expect(screen.queryByTestId("dispatch-full-prompt")).not.toBeInTheDocument();
    expect(onDispatch).not.toHaveBeenCalled();
  });

  it("raises the same prompt when a click came back refused", () => {
    // The machine filled between the poll and the press, so the state said nothing and
    // the server did. The reader gets the same three answers either way.
    renderPanel({ state: state(), dispatchRefusal: refusal });

    const prompt = screen.getByTestId("dispatch-full-prompt");
    expect(prompt).toHaveTextContent(holders);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("does not prompt on an idle machine", () => {
    const { onDispatch } = renderPanel({ state: state() });

    fireEvent.click(screen.getByRole("button", { name: /^▶ Dispatch/ }));

    expect(screen.queryByTestId("dispatch-full-prompt")).not.toBeInTheDocument();
    expect(onDispatch).toHaveBeenCalledWith({});
  });

  it("offers nothing to queue for a refusal a queue would not fix", () => {
    // A dirty tree is not made queueable by waiting: the server refuses it at start
    // time too, and offering the button would teach the operator that the control lies.
    renderPanel({
      dispatchRefusal: { reason: "dirty_tree", message: "The project has uncommitted changes." },
    });

    expect(screen.queryByTestId("dispatch-queue-it")).not.toBeInTheDocument();
    expect(screen.queryByTestId("dispatch-over-ceiling")).not.toBeInTheDocument();
  });

  it("reports a queued dispatch as queued rather than as a run that started", () => {
    renderPanel({
      queuedNotice: "Queued for the next free slot — place 2 in line. Nothing has started yet.",
    });

    const notice = screen.getByTestId("dispatch-queued-notice");
    expect(notice).toHaveTextContent("place 2 in line");
    expect(notice).toHaveTextContent("Nothing has started");
    // Not an alert, and not in the refusal box: a queued dispatch is the request being
    // accepted, and orange-boxing it would read as a failure.
    expect(notice).toHaveAttribute("role", "status");
  });
});

/**
 * The pull mode's arming control (task-462).
 *
 * Rendered through `DispatchSettings` rather than against `PullModeControl` directly,
 * because "the person sees this on the dispatch page" is part of what ac-5 asks for and
 * a component test that mounted it alone would pass with it wired to nothing.
 */
function renderPull(value: DispatchStateView, busy = false) {
  const onArm = vi.fn(async (_choice: PullArmChoice) => undefined);
  const onDisarm = vi.fn(async () => undefined);
  render(
    <DispatchSettings
      state={value}
      busy={busy}
      error={null}
      onEnable={vi.fn(async () => undefined)}
      onDisable={vi.fn(async () => undefined)}
      onArm={onArm}
      onDisarm={onDisarm}
    />,
  );
  return { onArm, onDisarm };
}

function pull(overrides: Partial<NonNullable<DispatchStateView["pull"]>> = {}) {
  return {
    armed: false,
    arming_id: "",
    armed_by: "",
    armed_at: "",
    bound_kind: "",
    bound: "",
    starts_used: 0,
    starts_left: null,
    merge_mode: null,
    merge_mode_phrase: "",
    next_task_id: "",
    next_task_title: "",
    last_state: "",
    last_detail: "",
    ...overrides,
  };
}

describe("arming the pull mode (task-462)", () => {
  it("sends the bound the person chose, and nothing it made up", () => {
    const { onArm } = renderPull(state({ pull: pull() }));

    fireEvent.change(screen.getByLabelText("Number of starts"), { target: { value: "5" } });
    fireEvent.click(screen.getByTestId("pull-mode-arm"));

    expect(onArm).toHaveBeenCalledWith({
      bound_kind: "starts",
      starts: 5,
      until: null,
      merge_mode: null,
    });
  });

  it("opens on the narrowest bound, never the open-ended one", () => {
    // The default is not neutral and does not have to be -- it has to be safe. Somebody
    // who presses Arm without reading spends three starts, not their evening.
    renderPull(state({ pull: pull() }));

    expect(screen.getByLabelText(/this many starts/i)).toBeChecked();
    expect(screen.getByLabelText(/keep going until I disarm it/i)).not.toBeChecked();
  });

  it("offers the open-ended bound as an explicit choice", () => {
    const { onArm } = renderPull(state({ pull: pull() }));

    fireEvent.click(screen.getByLabelText(/keep going until I disarm it/i));
    fireEvent.click(screen.getByTestId("pull-mode-arm"));

    expect(onArm).toHaveBeenCalledWith(
      expect.objectContaining({ bound_kind: "open", starts: null }),
    );
  });

  it("will not arm an until-bound with no moment named", () => {
    renderPull(state({ pull: pull() }));

    fireEvent.click(screen.getByLabelText(/this moment/i));

    expect(screen.getByTestId("pull-mode-arm")).toBeDisabled();
  });

  it("says what the chosen merge mode will do to every branch it produces", () => {
    // ac-5's second half. The sentence is outside the pulldown as well as in it: an
    // <option> is read once, and this is the sentence the decision turns on.
    renderPull(withMergeModes({ pull: pull() }));

    fireEvent.change(screen.getByLabelText("Merge mode"), { target: { value: "automerge" } });

    expect(screen.getByTestId("pull-mode-consequence")).toHaveTextContent(
      "Each pulled run merges itself on a green gate",
    );
  });

  it("says review hands off to you, so the two read differently", () => {
    renderPull(withMergeModes({ pull: pull() }));

    fireEvent.change(screen.getByLabelText("Merge mode"), { target: { value: "review" } });

    expect(screen.getByTestId("pull-mode-consequence")).toHaveTextContent(
      "Each pulled run hands off for your review",
    );
  });

  it("names what an armed pull mode does, in the server's words", () => {
    renderPull(
      state({
        pull: pull({
          armed: true,
          armed_by: "Jeff Posey",
          bound: "1 of 3 starts used",
          merge_mode: "automerge",
          merge_mode_phrase: "Merges itself on a green gate",
        }),
      }),
    );

    expect(screen.getByTestId("pull-mode-bound")).toHaveTextContent(
      "Armed by Jeff Posey · 1 of 3 starts used · Merges itself on a green gate",
    );
  });

  it("cannot be armed while dispatch is off for the project", () => {
    renderPull(state({ project_enabled: false, pull: pull() }));

    expect(screen.getByTestId("pull-mode-arm")).toBeDisabled();
  });

  it("says how the last arming ended, rather than going quiet", () => {
    renderPull(
      state({
        pull: pull({ last_state: "faulted", last_detail: "3 starts in a row failed to launch" }),
      }),
    );

    expect(screen.getByTestId("pull-mode-last")).toHaveTextContent("faulted");
    expect(screen.getByTestId("pull-mode-last")).toHaveTextContent("failed to launch");
  });
});

describe("the pull mode while it is armed", () => {
  const live = () =>
    state({
      pull: pull({
        armed: true,
        arming_id: "arm_abc",
        armed_by: "Jeff Posey",
        bound: "1 of 3 starts used",
        starts_used: 1,
        starts_left: 2,
        next_task_id: "task-077",
        next_task_title: "The one it would start",
      }),
    });

  it("names who armed it and what is left of the bound", () => {
    renderPull(live());

    expect(screen.getByTestId("pull-mode-bound")).toHaveTextContent("Jeff Posey");
    expect(screen.getByTestId("pull-mode-bound")).toHaveTextContent("1 of 3 starts used");
  });

  it("names what it would start next, in time to reorder the queue", () => {
    renderPull(live());

    expect(screen.getByTestId("pull-mode-next")).toHaveTextContent("task-077");
    expect(screen.getByTestId("pull-mode-next")).toHaveTextContent("The one it would start");
  });

  it("offers Disarm with no ceremony, and says it kills nothing", () => {
    // The kill-switch rule this file already follows for Disable: one click, no
    // confirmation, no reason demanded. Plus the one fact a person hesitates over.
    const { onDisarm } = renderPull(live());

    fireEvent.click(screen.getByTestId("pull-mode-disarm"));

    expect(onDisarm).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("pull-mode")).toHaveTextContent("Runs already going keep running");
  });

  it("offers no arming form while it is already armed", () => {
    renderPull(live());

    expect(screen.queryByTestId("pull-mode-arm")).not.toBeInTheDocument();
  });

  it("is absent entirely when the page supplies no handlers", () => {
    // The settings page is the only surface that arms, and a control wired to nothing
    // is a button that silently does nothing.
    render(
      <DispatchSettings
        state={state({ pull: pull() })}
        onEnable={vi.fn(async () => undefined)}
        onDisable={vi.fn(async () => undefined)}
      />,
    );

    expect(screen.queryByTestId("pull-mode")).not.toBeInTheDocument();
  });
});

/**
 * A dispatch of this task that is already waiting for a free slot (task-476).
 *
 * The panel used to offer Dispatch here, which the server answers `already_queued` to.
 * These assert on the sentence and on the id the click sends, never on an attribute.
 */
describe("a dispatch already waiting for a slot", () => {
  function queued(overrides: Partial<QueuedDispatchState> = {}): QueuedDispatchState {
    return {
      queue_id: "q_e1aeca49780f",
      position: 1,
      queued_at: "2026-09-19T15:29:00Z",
      queued_by: "Jeff Posey",
      source: "manual",
      status: "queued",
      detail: "",
      paused_by: "",
      ...overrides,
    };
  }

  it("says a dispatch is waiting instead of offering a second one", () => {
    renderPanel({ queuedDispatch: queued() });

    expect(screen.getByText(/waiting for the next free slot/i)).toBeInTheDocument();
    expect(screen.getByText(/nothing has started yet/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^dispatch/i })).toBeNull();
  });

  it("names the place in line when the entry is not next", () => {
    renderPanel({ queuedDispatch: queued({ position: 3 }) });

    expect(screen.getByText(/place 3 in line/i)).toBeInTheDocument();
  });

  it("cancels the queue entry, by the id the cancel route takes", () => {
    const { onCancel } = renderPanel({ queuedDispatch: queued() });

    fireEvent.click(screen.getByRole("button", { name: /cancel the queued dispatch/i }));

    expect(onCancel).toHaveBeenCalledWith("q_e1aeca49780f");
  });

  it("says why nothing is being tried when an incident is holding the start off", () => {
    renderPanel({ queuedDispatch: queued({ paused_by: "inc_7ffcc0210a984e39" }) });

    expect(screen.getByText(/inc_7ffcc0210a984e39 is open/i)).toBeInTheDocument();
    expect(screen.getByText(/keeps its place in line/i)).toBeInTheDocument();
  });

  it("withholds the cancel once a tick has taken the entry", () => {
    renderPanel({ queuedDispatch: queued({ status: "starting" }) });

    expect(screen.getByText(/is starting now/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /cancel the queued dispatch/i })).toBeDisabled();
    expect(screen.getByText(/too late to take it out of the queue/i)).toBeInTheDocument();
  });

  it("leaves a task with no waiting entry exactly as it was", () => {
    renderPanel({ queuedDispatch: null });

    expect(screen.getByRole("button", { name: /dispatch/i })).toBeEnabled();
    expect(screen.queryByText(/waiting for the next free slot/i)).toBeNull();
  });
});
