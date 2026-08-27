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
  type DispatchEnableTarget,
  type DispatchOptions,
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
  const onDispatch = vi.fn(async (_options?: DispatchOptions) => true);
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
    renderPanel({
      dispatchRefusal: {
        reason: "concurrency_limit",
        message:
          "This machine allows 2 concurrent run(s) and 2 are active: " +
          "run_aa11 on agentjobs/task-150 (running), run_bb22 on agentjobs/task-151 (starting). " +
          "Refused rather than queued: a queue turns this click into a promise to spend " +
          "money later, when nobody is watching. Cancel one of those runs, or dispatch " +
          "this again once one finishes.",
        suggestedAction:
          "Cancel one of the runs named above, wait for one to finish, or raise " +
          "limits.max_concurrent_runs in ~/.agentjobs/dispatch.yaml.",
      },
    });

    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("run_aa11 on agentjobs/task-150 (running)");
    expect(alert).toHaveTextContent("run_bb22 on agentjobs/task-151 (starting)");
    expect(alert).toHaveTextContent("Cancel one of the runs named above");
    // The ceiling is a number now, so nothing on this surface may call it "the only slot".
    expect(alert).not.toHaveTextContent(/only slot/i);
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
    expect(panel).toHaveTextContent(/Runner\s*claude-opus-5\s*from group\s*default/);
    expect(panel).toHaveTextContent(/authorised by\s*Jeff Posey/i);
  });

  it("offers this machine's groups, with the project's own answer spelled out", () => {
    renderPanel({ state: grouped() });

    const select = screen.getByLabelText("Group");
    expect([...select.querySelectorAll("option")].map((option) => option.textContent)).toEqual([
      // Groups, and a way to not choose one. Nothing else.
      //
      // This read "Project default — group default → claude-opus-5" until Jeff reviewed
      // it on 2026-08-24: it said "default" twice, ate the row's width, and offered
      // `claude-opus-5` -- a *runner* -- inside a select labelled Group. "just list the
      // damn groups in the pulldown for the group pulldown, dont add all that into it,
      // use the text below for mor info". What the project resolves to is already in
      // the sentence beside the button, which is where it belongs: after the choice.
      "Project default",
      "big",
      "default",
    ]);
    // Nothing pre-picked: a value the human did not choose is never sent as one.
    expect(select).toHaveValue("");
  });

  it("sends the group the human picked, and only then", async () => {
    const { onDispatch } = renderPanel({ state: grouped() });

    fireEvent.click(screen.getByRole("button", { name: /dispatch/i }));
    await waitFor(() => expect(onDispatch).toHaveBeenLastCalledWith({}));

    fireEvent.change(screen.getByLabelText("Group"), { target: { value: "big" } });
    fireEvent.click(screen.getByRole("button", { name: /dispatch/i }));

    await waitFor(() => expect(onDispatch).toHaveBeenLastCalledWith({ group: "big" }));
  });

  it("carries the group alongside a brief on the task that needed one", async () => {
    const { onDispatch } = renderPanel({ state: grouped(), recordCanBrief: false });

    fireEvent.change(screen.getByLabelText("Group"), { target: { value: "big" } });
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

    fireEvent.change(screen.getByLabelText("Group"), { target: { value: "big" } });

    const panel = screen.getByRole("region", { name: "Dispatch" });
    expect(panel).toHaveTextContent(/Runner chosen from group\s*big/);
    // The pulldown still spells out the project's default in its first option, which is
    // the whole point of that option -- what must not survive is the *claim* that
    // claude-opus-5 is what this click will run.
    expect(panel).not.toHaveTextContent(/Runner\s*claude-opus-5/);
  });

  it("offers no group control at all on a machine that defines none", () => {
    // sc-4: a project that never had a group reads exactly as it did before they
    // existed -- no pulldown, and the same sentence beside the button.
    renderPanel();

    expect(screen.queryByLabelText("Group")).toBeNull();
    expect(screen.getByRole("region", { name: "Dispatch" })).toHaveTextContent(
      /Runner\s*claude-session, posture\s*supervised, authorised by\s*Jeff Posey/,
    );
  });
});

/**
 * A project whose ceiling has been raised, so there is genuinely a choice to make.
 *
 * `posture_merge_policies` is present because the server always sends it -- it is the
 * fixed task-021 mapping, not configuration. A fixture that omitted it would be testing
 * a state the API cannot produce.
 */
function withPostures(overrides: Partial<DispatchStateView> = {}): DispatchStateView {
  return state({
    posture: "auto",
    max_posture: "autonomous",
    offerable_postures: ["read_only", "supervised", "auto", "autonomous"],
    posture_merge_policies: {
      read_only: "none",
      supervised: "review",
      auto: "review",
      autonomous: "automatic",
    },
    finish_enabled: true,
    ...overrides,
  });
}

describe("choosing a posture for one dispatch (task-307)", () => {
  it("offers what the server permits, each saying what it does to the branch", () => {
    renderPanel({ state: withPostures() });

    const select = screen.getByLabelText("Envelope");
    expect([...select.querySelectorAll("option")].map((option) => option.textContent)).toEqual([
      "Project default",
      "read_only — no shell, nothing to merge",
      "supervised — stops for your review before merging",
      "auto — stops for your review before merging",
      "autonomous — merges its own work when the gate passes, no review",
    ]);
    // Nothing pre-picked, so a posture the human did not choose is never sent as one.
    expect(select).toHaveValue("");
  });

  it("offers only what is at or below the ceiling, because the API refuses the rest", () => {
    // The refusal exists server-side either way (`posture_above_ceiling`). Populating
    // from `offerable_postures` is what makes it unreachable by clicking, which is the
    // difference between a control that is bounded and one that lies.
    renderPanel({
      state: withPostures({
        max_posture: "auto",
        offerable_postures: ["read_only", "supervised", "auto"],
      }),
    });

    const options = [...screen.getByLabelText("Envelope").querySelectorAll("option")].map(
      (option) => option.textContent,
    );
    expect(options).not.toContain(
      "autonomous — merges its own work when the gate passes, no review",
    );
    expect(options).toHaveLength(4);
  });

  it("sends the posture the human picked, and only then", async () => {
    const { onDispatch } = renderPanel({ state: withPostures() });

    fireEvent.click(screen.getByRole("button", { name: /dispatch/i }));
    await waitFor(() => expect(onDispatch).toHaveBeenLastCalledWith({}));

    fireEvent.change(screen.getByLabelText("Envelope"), { target: { value: "autonomous" } });
    fireEvent.click(screen.getByRole("button", { name: /dispatch/i }));

    await waitFor(() => expect(onDispatch).toHaveBeenLastCalledWith({ posture: "autonomous" }));
  });

  it("carries the posture alongside a group and a brief", async () => {
    const { onDispatch } = renderPanel({
      state: withPostures({ available_groups: ["big", "default"] }),
      recordCanBrief: false,
    });

    fireEvent.change(screen.getByLabelText("Group"), { target: { value: "big" } });
    fireEvent.change(screen.getByLabelText("Envelope"), { target: { value: "autonomous" } });
    fireEvent.change(screen.getByRole("textbox", { name: /say what the agent should do/i }), {
      target: { value: "Audit the guard chain." },
    });
    fireEvent.click(screen.getByRole("button", { name: /dispatch/i }));

    await waitFor(() =>
      expect(onDispatch).toHaveBeenCalledWith({
        group: "big",
        posture: "autonomous",
        note: "Audit the guard chain.",
      }),
    );
  });

  it("says what the chosen posture will do, not what the project's default would have", () => {
    renderPanel({ state: withPostures() });

    const panel = screen.getByRole("region", { name: "Dispatch" });
    expect(panel).toHaveTextContent(/posture\s*auto\s*—\s*stops for your review before merging/);

    fireEvent.change(screen.getByLabelText("Envelope"), { target: { value: "autonomous" } });

    expect(panel).toHaveTextContent(
      /posture\s*autonomous\s*—\s*merges its own work when the gate passes, no review/,
    );
  });

  it("offers autonomous disabled, and says why, where the project has no scripted finish", () => {
    // task-021: an autonomous merge runs through `agentjobs finish --posture-release`,
    // and a machine without it has no sanctioned mechanism for one. Granting the
    // envelope anyway produces a run told it may merge with no way to do it.
    renderPanel({ state: withPostures({ finish_enabled: false }) });

    const autonomous = [...screen.getByLabelText("Envelope").querySelectorAll("option")].find(
      (option) => (option as HTMLOptionElement).value === "autonomous",
    );
    expect(autonomous?.textContent).toBe("autonomous — needs finish.enabled on this project");
    expect(autonomous).toBeDisabled();
  });

  it("leaves the other postures alone when the finish is off", () => {
    renderPanel({ state: withPostures({ finish_enabled: false }) });

    const options = [...screen.getByLabelText("Envelope").querySelectorAll("option")];
    expect(options.filter((option) => option.hasAttribute("disabled"))).toHaveLength(1);
  });

  it("offers no posture control at all when the ceiling leaves nothing to choose", () => {
    // A project capped at its own default. A pulldown whose single option means "the
    // only thing that can happen" is furniture, and the panel must read exactly as it
    // did before this control existed.
    renderPanel({
      state: withPostures({
        posture: "read_only",
        max_posture: "read_only",
        offerable_postures: ["read_only"],
      }),
    });

    expect(screen.queryByLabelText("Envelope")).toBeNull();
    expect(screen.getByRole("region", { name: "Dispatch" })).toHaveTextContent(
      /posture\s*read_only\s*—\s*no shell, nothing to merge/,
    );
  });

  it("names pushing only where the project permits it", () => {
    // Per project and never the posture's (task-021), and false everywhere today -- so
    // a sentence repeating the universal default on every task would be noise.
    renderPanel({ state: withPostures() });
    expect(screen.getByRole("region", { name: "Dispatch" })).not.toHaveTextContent(/and pushes/);
  });

  it("says so where pushing is permitted, because that is the unrecoverable half", () => {
    renderPanel({ state: withPostures({ push: true }) });

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
      "group: big",
      "group: default",
      "runner: claude-session",
      "runner: claude-batch",
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
