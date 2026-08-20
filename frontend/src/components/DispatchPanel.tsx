import { useState, type ReactNode } from "react";

import type { ReviewIdentity } from "../api/generated";
import type { DispatchRunView, DispatchStateView } from "../api/types";

/**
 * Dispatch, in the browser: the button that starts an agent, the runs it produces,
 * and the switch that decides whether any of it is allowed here.
 *
 * The one rule that shapes everything in this file: **Dispatch is not Approve.**
 * Approving means "I agree with what you did"; dispatching means "spend money now, on
 * my machine, unattended". They are separate controls, in separate panels, with
 * different colours and different verbs, because a single button meaning both would
 * turn every approval into an implicit purchase (design decision D1).
 *
 * The third rule, and the newest: **one click.** The record is the brief. Pressing
 * Dispatch on a task whose spec is complete starts a run and nothing else happens --
 * the server writes the authorising entry itself, attributed to the person signed in
 * here. A box asking for text is a *special occasion*, and it appears only when the
 * record genuinely could not brief an agent. Before task-188 the guard demanded a
 * human-written newest log entry, which 72 of this project's 74 open tasks did not
 * have, so the ceremony was the default rather than the exception.
 *
 * The second rule is what this file *cannot* do. There is no runner editor here and
 * there is no way to reach one. The browser may point a project at a command a human
 * already wrote into `~/.agentjobs/dispatch.yaml`, and may switch that on and off; it
 * may never describe a new command. The API refuses it too -- this is the visible half
 * of a rule enforced in both places.
 */

const RUN_POLL_MS = 2_000;

/**
 * What to do about each refusal, in the second person.
 *
 * Keyed by the guard layer's stable `reason` codes rather than by message text. The
 * server sends a `suggested_action` for the dispatch endpoint's refusals and this map
 * covers the state endpoint's, which carries no such field; where both exist the
 * server's wins, because it can name the actual file on this machine -- except for the
 * reasons in `PAGE_REMEDY_REASONS`, where only the browser can name the control.
 */
export const REFUSAL_ACTIONS: Record<string, string> = {
  not_configured:
    "Dispatch is not set up on this machine. Create ~/.agentjobs/dispatch.yaml and define a runner before anything can start.",
  disabled: "The machine-wide switch is off. Set 'enabled: true' in ~/.agentjobs/dispatch.yaml.",
  sentinel:
    "Dispatch is switched off by the kill-switch file. Delete ~/.agentjobs/DISPATCH_DISABLED to allow runs again.",
  project_not_enabled: "This project is not enabled for dispatch. Turn it on under Dispatch.",
  unknown_runner:
    "This project names a runner this machine does not define. Pick one that exists, or add it by hand to the config file.",
  invalid_config: "The dispatch config could not be read. Fix the YAML, then reload.",
  not_human_clocked:
    "AgentJobs cannot tell who is clicking, so it has nobody to attribute this run to, and it will not sign one on your behalf. Configure a human under 'actors:' in .agentjobs/config.yaml — or use “Add a note” below to write the authorising entry yourself, then dispatch.",
  authorizer_not_human:
    "The identity this page is signed in as is not a human this project configures. A run has to be authorised by a person, and that rule is not configurable.",
  insufficient_record:
    "This task has no working specification, so there is nothing for an agent to go on. Say what it should do — what you write becomes the task's authorising entry.",
  no_causing_entry:
    "Nothing on this task was written by a human yet. Use “Add a note” below to write the entry that authorises this run, then dispatch.",
  task_closed: "Reopen the task before dispatching at it.",
  live_run_exists: "A run for this task is already going. Wait for it, or cancel it below.",
  concurrency_limit:
    "Every slot this machine allows is in use. The refusal above names the runs holding them and the task each is working; cancel one of those, or raise limits.max_concurrent_runs in ~/.agentjobs/dispatch.yaml.",
  dirty_tree: "The project's working tree has uncommitted changes. Commit or stash them first.",
  claim_lost: "Someone else took this task. Re-read it before deciding again.",
  owner_mismatch: "This task is owned by a different agent. Release it, or dispatch its owner.",
};

/**
 * Refusals whose remedy is a control on this page.
 *
 * The server's `suggested_action` normally wins, because it can name the actual file on
 * this machine. For these two it must not: the same sentence is read by the CLI and by
 * MCP, so it cannot say "press the button below" — and "the button below" is precisely
 * what the reader of *this* surface needs to be told. Task-185 was filed because the
 * refusal named a remedy the page did not offer; naming one it does offer is the fix,
 * and that sentence can only be written here.
 */
export const PAGE_REMEDY_REASONS = new Set([
  "no_causing_entry",
  "not_human_clocked",
  // The remedy is the textarea this panel renders, which only exists here. The
  // server's sentence has to stay readable by the CLI and by MCP, so it cannot say
  // "type it in the box".
  "insufficient_record",
]);

export type DispatchRefusal = {
  reason: string;
  message: string;
  suggestedAction?: string | null;
};

/** How often to re-read the runs list. Fast while something is running, never otherwise. */
export function runsPollInterval(runs: Array<DispatchRunView>): number | false {
  return runs.some((run) => run.live) ? RUN_POLL_MS : false;
}

/** Elapsed seconds as a human reads them. Server-computed, so no client clock is involved. */
export function formatElapsed(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "unknown";
  const whole = Math.max(0, Math.floor(seconds));
  const minutes = Math.floor(whole / 60);
  if (minutes < 1) return `${whole}s`;
  const hours = Math.floor(minutes / 60);
  if (hours < 1) return `${minutes}m ${String(whole % 60).padStart(2, "0")}s`;
  return `${hours}h ${String(minutes % 60).padStart(2, "0")}m`;
}

/** The word a human should read for a run's state, never the enum's own spelling. */
export function runStateLabel(run: DispatchRunView): string {
  if (run.live) return "Running";
  switch (run.outcome) {
    case "completed":
      return "Completed";
    case "cancelled":
      return "Cancelled";
    case "failed":
      return "Failed";
    case "timeout":
      return "Timed out";
    case "crashed":
      return "Crashed";
    case "interrupted":
      return "Interrupted";
    case "finished_without_handoff":
      return "Stopped without saying what it needs";
    default:
      return "Finished";
  }
}

/**
 * A refusal, with what to do about it.
 *
 * `answered` distinguishes the two kinds, and it is not decoration. A gate that was
 * already closed before anyone touched anything is *status*: it describes the world.
 * A refusal that came back from a button the human just pressed is an *alert*: it
 * describes their action. Announcing the first as an alert makes a screen reader
 * interrupt on every task page, and makes `getByRole("alert")` on any other page
 * ambiguous -- which is how this was found.
 */
function RefusalNote({ refusal, answered = true }: { refusal: DispatchRefusal; answered?: boolean }) {
  const action = PAGE_REMEDY_REASONS.has(refusal.reason)
    ? REFUSAL_ACTIONS[refusal.reason]
    : refusal.suggestedAction || REFUSAL_ACTIONS[refusal.reason];
  return (
    <div
      role={answered ? "alert" : "status"}
      data-refusal-reason={refusal.reason}
      className="rounded-lg border border-orange-600/50 bg-orange-950/30 p-3 text-sm text-orange-100"
    >
      <p>{refusal.message}</p>
      {action && <p className="mt-2 text-orange-200">{action}</p>}
    </div>
  );
}

export type DispatchPanelProps = {
  /** Machine and project gates. Null while it is still being read. */
  state: DispatchStateView | null;
  runs: Array<DispatchRunView>;
  /** True when this task's ball is with an agent and the task is open. */
  taskIsDispatchable: boolean;
  /**
   * Who the server would attribute this run's authorising entry to.
   *
   * Required, and not optional-with-a-fallback on purpose. The entry has to name a real
   * person; a run signed by "whoever the config happens to default to" looks like
   * evidence and is not. With no resolvable user the button is disabled and says why,
   * rather than being pressable into a refusal.
   */
  identity: ReviewIdentity;
  /**
   * Whether the task record could brief an agent that has never seen it.
   *
   * Computed by the caller from `spec.description`, which is the same field the server
   * checks. The server is the authority: if the two ever disagree, the dispatch comes
   * back refused with `insufficient_record` and this panel opens the box anyway, so a
   * drift between them costs a round trip rather than a wrong screen.
   */
  recordCanBrief: boolean;
  busy?: boolean;
  cancellingRunId?: string | null;
  /** The last refusal from pressing Dispatch, which the state endpoint cannot predict. */
  dispatchRefusal?: DispatchRefusal | null;
  /**
   * Start a run. `note` is the human's text, and is sent only when it was asked for.
   *
   * There is deliberately no always-present optional box feeding this. Adding an
   * instruction to a dispatch you are already making is task-162's feature, and voice
   * is task-172's; building a second input here would be something they had to remove.
   *
   * **Resolves `true` only when a run actually started.** The handler owns the refusal
   * -- it reads the guard's reason and renders it -- so it resolves rather than throws,
   * and this panel therefore cannot learn from the promise settling whether the click
   * worked. It has to be told, because the one thing it does on success is destroy the
   * only copy of what the human typed. `boolean` rather than `void` is what makes a
   * caller that forgets to say so a type error instead of a silently emptied textarea.
   */
  onDispatch: (note?: string) => Promise<boolean> | boolean;
  onCancel: (runId: string) => Promise<void> | void;
  /**
   * The output panel for one run, supplied rather than imported.
   *
   * It reads from the API on its own clock, and this file is otherwise given everything
   * it renders. Passing it in keeps that true, so the panel can still be rendered in a
   * test without a query client to answer for a surface the test is not about.
   */
  renderOutput?: (run: DispatchRunView) => ReactNode;
};

/**
 * The task-level dispatch surface.
 *
 * Renders whenever the task could be dispatched *or* has ever been dispatched, so a
 * finished run does not vanish from the page the moment the ball moves -- the record of
 * what ran is exactly what a human comes back to look at.
 */
export function DispatchPanel({
  state,
  runs,
  taskIsDispatchable,
  identity,
  recordCanBrief,
  busy = false,
  cancellingRunId = null,
  dispatchRefusal = null,
  onDispatch,
  onCancel,
  renderOutput,
}: DispatchPanelProps) {
  const [brief, setBrief] = useState("");

  if (!taskIsDispatchable && runs.length === 0) return null;
  // Silent on a machine where dispatch was never set up. There is nothing to switch on
  // and nothing to explain: putting a box on every task about a feature the owner has
  // not configured is clutter on every page, forever. Once a dispatch.yaml exists, a
  // closed gate is worth naming, because then it is a thing the reader can act on.
  if (!state?.configured && runs.length === 0) return null;

  const gateRefusal: DispatchRefusal | null =
    state && !state.can_dispatch && state.refusal
      ? { reason: state.refusal.reason, message: state.refusal.message }
      : null;
  const offerButton = taskIsDispatchable && Boolean(state?.can_dispatch);
  const user = identity.ok ? identity.user : null;
  // The special occasion, from either direction: the record looks insufficient here, or
  // the server said so when the button was pressed. Honouring the server's answer as
  // well as the local one means the box still appears if the two ever drift apart.
  const askForBrief = !recordCanBrief || dispatchRefusal?.reason === "insufficient_record";

  return (
    <section
      className="space-y-4 rounded-xl border-2 border-sky-700/50 bg-sky-950/30 p-4 min-[820px]:p-6"
      aria-label="Dispatch"
      data-dispatch-ready={state?.can_dispatch ? "yes" : "no"}
      data-dispatch-asks-for-brief={askForBrief ? "yes" : "no"}
    >
      <div>
        <h2 className="text-lg font-semibold text-sky-300">Dispatch</h2>
        <p className="mt-1 text-sm text-dark-muted">
          Starts an agent on this task, on this machine, now. This is not approval — it
          spends tokens and lets a process write to the project.
        </p>
      </div>

      {offerButton && !user && (
        // Disabled rather than pressable-into-a-refusal. The run needs somebody's name
        // on it, and the page knows before the click that it has none to offer.
        <div
          role="status"
          data-refusal-reason="no_signed_in_user"
          className="rounded-lg border border-orange-600/50 bg-orange-950/30 p-3 text-sm text-orange-100"
        >
          <p>
            Nobody is signed in, so there is no one to attribute this run to. AgentJobs
            will not sign it for you.
          </p>
          <p className="mt-2 text-orange-200">{identity.detail}</p>
        </div>
      )}

      {offerButton && user && !askForBrief && (
        <div className="mobile-action-row flex flex-wrap items-center gap-3">
          <button
            type="button"
            disabled={busy}
            onClick={() => void onDispatch()}
            className="touch-target rounded-lg bg-sky-600 px-4 font-semibold text-white hover:bg-sky-500 disabled:opacity-60"
          >
            ▶ Dispatch — start an agent now
          </button>
          <DispatchRunnerNote state={state} user={user} />
        </div>
      )}

      {offerButton && user && askForBrief && (
        // Rare by design: measured against this project's backlog on 2026-08-20 it fires
        // on none of the 74 open tasks. Rendered inline rather than behind a disclosure
        // because it is the thing standing between the reader and the run, and a
        // collapsed control is one more thing to discover.
        <form
          className="space-y-3"
          onSubmit={(event) => {
            event.preventDefault();
            const value = brief.trim();
            if (!value) return;
            // Cleared only when a run started. This text exists nowhere else -- it has
            // not been saved to the task, and after task-172 it may have been dictated
            // rather than typed -- so a refusal that empties the box costs the sentence
            // rather than a click. Which is exactly what shipped and was caught in
            // review: the handler resolves on refusal too, so a `.then(clear)` cleared
            // on every outcome and the rejection branch written to prevent it was dead.
            void Promise.resolve(onDispatch(value)).then(
              (started) => {
                if (started) setBrief("");
              },
              () => undefined,
            );
          }}
        >
          <label htmlFor="dispatch-brief" className="block text-sm font-semibold">
            This task has no specification — say what the agent should do
          </label>
          <textarea
            id="dispatch-brief"
            required
            rows={4}
            value={brief}
            onChange={(event) => setBrief(event.target.value)}
            placeholder="What the agent should do…"
            className="w-full rounded-lg border border-dark-border bg-dark-bg p-3 text-dark-text focus:border-sky-500 focus:outline-none"
          />
          <p className="text-sm text-dark-muted">
            Saved to the task as a note by{" "}
            <strong className="text-dark-text">{user}</strong>, and that note is what
            authorises the run.
          </p>
          <div className="mobile-action-row flex flex-wrap items-center gap-3">
            <button
              type="submit"
              disabled={busy || !brief.trim()}
              className="touch-target rounded-lg bg-sky-600 px-4 font-semibold text-white hover:bg-sky-500 disabled:opacity-60"
            >
              ▶ Dispatch — start an agent now
            </button>
            <DispatchRunnerNote state={state} user={user} />
          </div>
        </form>
      )}

      {taskIsDispatchable && gateRefusal && <RefusalNote refusal={gateRefusal} answered={false} />}
      {dispatchRefusal && <RefusalNote refusal={dispatchRefusal} />}

      <DispatchRunList
        runs={runs}
        cancellingRunId={cancellingRunId}
        onCancel={onCancel}
        renderOutput={renderOutput}
      />
    </section>
  );
}

/** What the run will be, and whose name goes on it. Both worth reading before clicking. */
function DispatchRunnerNote({ state, user }: { state: DispatchStateView | null; user: string }) {
  if (!state?.runner) return null;
  return (
    <span className="text-sm text-dark-muted">
      Runner <strong className="text-dark-text">{state.runner}</strong>, posture{" "}
      <strong className="text-dark-text">{state.posture}</strong>, authorised by{" "}
      <strong className="text-dark-text">{user}</strong>
    </span>
  );
}

export function DispatchRunList({
  runs,
  cancellingRunId = null,
  onCancel,
  renderOutput,
}: {
  runs: Array<DispatchRunView>;
  cancellingRunId?: string | null;
  onCancel: (runId: string) => Promise<void> | void;
  renderOutput?: (run: DispatchRunView) => ReactNode;
}) {
  if (runs.length === 0) return null;
  return (
    <div className="rounded-lg border border-dark-border bg-dark-surface" data-testid="dispatch-runs">
      <h3 className="border-b border-dark-border p-3 text-sm font-semibold">Runs</h3>
      <ul className="divide-y divide-dark-border">
        {runs.map((run) => (
          <li
            key={run.run_id}
            data-run-id={run.run_id}
            data-run-live={run.live ? "yes" : "no"}
            className="p-3"
          >
            <div className="flex flex-col gap-2 min-[820px]:flex-row min-[820px]:items-center min-[820px]:justify-between">
            <div className="min-w-0 text-sm">
              <div className="flex flex-wrap items-center gap-2">
                <span
                  className={`rounded px-2 py-0.5 text-xs font-semibold ${
                    run.live ? "bg-sky-900 text-sky-200" : "bg-slate-700 text-slate-200"
                  }`}
                >
                  {runStateLabel(run)}
                </span>
                <span className="font-mono text-xs text-dark-muted">{run.run_id}</span>
                <span className="text-xs text-dark-muted">{run.mode}</span>
              </div>
              <div className="mt-1 text-dark-muted">
                {run.live ? "Running for " : "Ran for "}
                {formatElapsed(run.elapsed_seconds)}
              </div>
            </div>
            <div className="mobile-action-row flex flex-wrap gap-2">
              <a
                href={run.output_url}
                target="_blank"
                rel="noreferrer"
                className="touch-target rounded-lg border border-dark-border px-3 text-sm text-blue-300 hover:bg-dark-border"
              >
                View output
              </a>
              {run.live && (
                <button
                  type="button"
                  disabled={cancellingRunId === run.run_id}
                  onClick={() => void onCancel(run.run_id)}
                  className="touch-target rounded-lg bg-red-700 px-3 text-sm font-semibold text-white hover:bg-red-600 disabled:opacity-60"
                >
                  Cancel run
                </button>
              )}
            </div>
            </div>
            {renderOutput?.(run)}
          </li>
        ))}
      </ul>
    </div>
  );
}

export type DispatchSettingsProps = {
  state: DispatchStateView | null;
  busy?: boolean;
  error?: string | null;
  onEnable: (runner: string | null) => Promise<void> | void;
  onDisable: () => Promise<void> | void;
};

/**
 * The per-project switch, and the machine state it depends on.
 *
 * Disable is rendered unconditionally whenever the project is on: no confirmation, no
 * second click, no explanation demanded first. A kill switch you cannot reach is not
 * one, and one that argues with you is worse than none.
 *
 * The runner is a `<select>` over names this machine defines, never a text field. A
 * free-text runner would let a browser name a command, which is precisely the thing the
 * whole gate design exists to prevent -- and the API would refuse it anyway.
 */
export function DispatchSettings({
  state,
  busy = false,
  error = null,
  onEnable,
  onDisable,
}: DispatchSettingsProps) {
  const [runner, setRunner] = useState<string>("");
  if (!state) {
    return <p className="text-dark-muted">Reading this machine's dispatch configuration…</p>;
  }

  const runners = state.available_runners ?? [];
  const chosen = runner || state.runner || runners[0] || "";

  return (
    <section className="space-y-6" aria-label="Dispatch settings">
      <div className="rounded-xl border border-dark-border bg-dark-surface p-4 min-[820px]:p-6">
        <h2 className="text-lg font-semibold">Dispatch for {state.project_id}</h2>
        <p className="mt-1 text-sm text-dark-muted">
          Dispatch lets a click in this browser start an agent process on this machine.
          It is off until every gate below is open.
        </p>

        <dl className="mt-4 grid gap-px overflow-hidden rounded-lg border border-dark-border bg-dark-border sm:grid-cols-2">
          <Gate label="Configured on this machine" open={state.configured} detail={state.config_path} />
          <Gate
            label="Machine-wide switch"
            open={state.master_enabled}
            detail={state.master_enabled ? "enabled: true" : "enabled: false — edit the config file"}
          />
          <Gate
            label="Kill switch"
            open={!state.sentinel_active}
            detail={state.sentinel_active ? `${state.sentinel_file} exists` : "no sentinel file"}
          />
          <Gate
            label="This project"
            open={state.project_enabled}
            detail={state.runner ? `runner: ${state.runner}` : "no runner chosen"}
          />
        </dl>

        {/* Read-only, and deliberately not a toggle. Auto-dispatch is the one setting
            that lets a click start a run with no further click, so it is changed by
            editing the machine-local file and nowhere else. Shown here because a human
            is entitled to know from the browser whether it is armed. */}
        <p className="mt-4 text-sm" data-auto-dispatch={state.auto_dispatch ? "on" : "off"}>
          <span className="text-dark-muted">Auto-dispatch on approval: </span>
          <strong className={state.auto_dispatch ? "text-orange-300" : "text-dark-text"}>
            {state.auto_dispatch ? "on" : "off"}
          </strong>
          <span className="text-dark-muted">
            {state.auto_dispatch
              ? " — approving a task here starts an agent immediately. Change it in the config file."
              : " — approving records the approval and starts nothing. Change it in the config file."}
          </span>
        </p>

        {state.refusal && (
          <div className="mt-4">
            <RefusalNote
              refusal={{ reason: state.refusal.reason, message: state.refusal.message }}
              answered={false}
            />
          </div>
        )}

        <div className="mt-5 flex flex-wrap items-end gap-3">
          {state.project_enabled ? (
            <button
              type="button"
              disabled={busy}
              onClick={() => void onDisable()}
              className="touch-target rounded-lg bg-red-700 px-4 font-semibold text-white hover:bg-red-600 disabled:opacity-60"
            >
              Disable dispatch for this project
            </button>
          ) : (
            <>
              <div>
                <label htmlFor="dispatch-runner" className="block text-sm font-semibold">
                  Runner
                </label>
                <select
                  id="dispatch-runner"
                  value={chosen}
                  disabled={busy || runners.length === 0}
                  onChange={(event) => setRunner(event.target.value)}
                  className="mt-1 rounded-lg border border-dark-border bg-dark-bg p-2 text-dark-text"
                >
                  {runners.length === 0 && <option value="">none defined</option>}
                  {runners.map((name) => (
                    <option key={name} value={name}>
                      {name}
                    </option>
                  ))}
                </select>
              </div>
              <button
                type="button"
                disabled={busy || runners.length === 0}
                onClick={() => void onEnable(chosen || null)}
                className="touch-target rounded-lg bg-emerald-600 px-4 font-semibold text-white hover:bg-emerald-700 disabled:opacity-60"
              >
                Enable dispatch for this project
              </button>
            </>
          )}
        </div>

        <p className="mt-4 text-xs text-dark-muted">
          Runners are defined by hand in <code>{state.config_path}</code> and never from
          this page. This switch chooses among commands that already exist on this
          machine; it cannot describe a new one.
        </p>
        {error && (
          <p role="alert" className="mt-3 text-sm text-red-300">
            {error}
          </p>
        )}
      </div>
    </section>
  );
}

function Gate({ label, open, detail }: { label: string; open: boolean; detail: string }) {
  return (
    <div className="bg-dark-surface p-3" data-gate={label} data-gate-open={open ? "yes" : "no"}>
      <dt className="text-xs uppercase text-dark-muted">{label}</dt>
      <dd className="mt-1 text-sm">
        <span className={open ? "text-emerald-300" : "text-orange-300"}>{open ? "Open" : "Closed"}</span>
        <span className="ml-2 break-all text-xs text-dark-muted">{detail}</span>
      </dd>
    </div>
  );
}
