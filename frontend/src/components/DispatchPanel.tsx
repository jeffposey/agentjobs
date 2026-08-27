import { useState, type ReactNode } from "react";

import type { ReviewIdentity } from "../api/generated";
import type { DispatchPosture, DispatchRunView, DispatchStateView } from "../api/types";

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
  task_on_hold:
    "A person put this task on hold, and the release condition is in the panel above. Resume it there before dispatching an agent at it.",
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

/**
 * What a human chose for this one dispatch, on top of what the project already says.
 *
 * One object rather than a widening argument list, because this is the shape the next
 * per-dispatch choice arrives in: task-307 adds `posture` here and changes nothing else
 * about how a dispatch is started.
 *
 * Every field is optional, and an omitted one means "whatever the project resolves to".
 * The browser never restates a default it did not choose, so a dispatch with nothing
 * picked posts exactly the body it posted before this control existed -- which is what
 * keeps a machine with no groups behaving as it did before they were added.
 */
export type DispatchOptions = {
  /** Runner group to choose from. Outranks the project's own group and its runner. */
  group?: string;
  /**
   * What this one run may do. Outranks a posture on the task record and the project's
   * default, and is refused above the project's ceiling (task-307, task-308).
   */
  posture?: DispatchPosture;
  /** The human's brief, sent only when the panel asked for one. */
  note?: string;
};

/**
 * Where this project's runs come from, in the words the CLI already uses for it.
 *
 * The server resolves this rather than the browser: a project pointed at a group names
 * no runner of its own, and a page that re-implemented the precedence ladder to work
 * out which member wins would be the one place in the system that could disagree with
 * the dispatcher about what actually runs.
 */
function projectDefaultLabel(_state: DispatchStateView | null): string {
  // Two words, and deliberately not the resolution. This is the *Group* select, so an
  // option reading "default → claude-opus-5" offers a runner as though it were a group
  // -- and spends the row's width saying "default" twice to do it. What the project
  // actually resolves to is already stated in the sentence beside the button, which is
  // where a clarification belongs: after the choice, not inside it. Jeff, 2026-08-24.
  return "Project default";
}

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
  /**
   * A scripted finish is working on this task's branch right now (task-321).
   *
   * Disables the button and says so, rather than leaving it pressable into a refusal.
   * The server refuses it anyway -- the finish holds the task's run lock, and a dispatch
   * asking for it is told `live_run_exists` -- so this changes nothing about what can
   * happen; it changes what the page tells somebody *before* they press. That matters
   * here more than for most refusals, because the reason the button is unavailable is a
   * thing they themselves started thirty seconds ago by pressing Approve.
   */
  finishLive?: boolean;
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
   *
   * `options.note` is the human's text; `options.group` is the runner group they picked
   * for this run. Both are omitted unless chosen -- see {@link DispatchOptions}.
   */
  onDispatch: (options?: DispatchOptions) => Promise<boolean> | boolean;
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
  finishLive = false,
  cancellingRunId = null,
  dispatchRefusal = null,
  onDispatch,
  onCancel,
  renderOutput,
}: DispatchPanelProps) {
  const [brief, setBrief] = useState("");
  // Empty means "the project's own", and it is deliberately not pre-filled with the
  // project's group: a value the human did not pick must not be sent as though they
  // had. Reset per page load rather than remembered -- "audit this one with the big
  // model" is a decision about one task, and a sticky override would silently apply it
  // to the next one.
  const [group, setGroup] = useState("");
  // Same rule as `group`, and it matters more here: an override that stuck would carry
  // "let this one merge itself" onto the next task the reader opened, which is the one
  // sticky default nobody would want.
  const [posture, setPosture] = useState("");

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
  // One name for "the button must not be pressable", so the plain button and the
  // brief form cannot drift apart on what disables them.
  const blocked = busy || finishLive;
  /** What this click will send. Omitted keys are the point -- see `DispatchOptions`. */
  const options = (note?: string): DispatchOptions => ({
    ...(group ? { group } : {}),
    // Narrowed rather than validated: every value this can hold came out of
    // `offerable_postures`, which the server derives from the same `Posture` enum the
    // generated type is generated from. A posture the API would reject cannot reach
    // here, and if one ever did the API refuses it under `posture_above_ceiling`.
    ...(posture ? { posture: posture as DispatchPosture } : {}),
    ...(note ? { note } : {}),
  });

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

      {offerButton && finishLive && (
        // Status rather than alert: this describes the world, not an act the reader
        // just took. The same distinction `RefusalNote` draws, and for the same reason
        // -- a screen reader must not interrupt on every poll of a live finish.
        <div
          role="status"
          data-refusal-reason="finish_in_progress"
          className="rounded-lg border border-indigo-600/50 bg-indigo-950/30 p-3 text-sm text-indigo-100"
        >
          <p>
            A finish is working on this task's branch right now. It holds the task, so
            an agent cannot be started on it until it ends.
          </p>
          <p className="mt-2 text-indigo-200">
            Watch it above. If it stops without merging, the task record says where, and
            this button comes back.
          </p>
        </div>
      )}

      {offerButton && user && !askForBrief && (
        <div className="mobile-action-row flex flex-wrap items-center gap-3">
          <button
            type="button"
            disabled={blocked}
            onClick={() => void onDispatch(options())}
            className="touch-target rounded-lg bg-sky-600 px-4 font-semibold text-white hover:bg-sky-500 disabled:opacity-60"
          >
            ▶ Dispatch — start an agent now
          </button>
          <DispatchGroupChoice state={state} value={group} busy={blocked} onChange={setGroup} />
          <DispatchPostureChoice state={state} value={posture} busy={blocked} onChange={setPosture} />
          <DispatchRunnerNote state={state} user={user} group={group} posture={posture} />
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
            void Promise.resolve(onDispatch(options(value))).then(
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
              disabled={blocked || !brief.trim()}
              className="touch-target rounded-lg bg-sky-600 px-4 font-semibold text-white hover:bg-sky-500 disabled:opacity-60"
            >
              ▶ Dispatch — start an agent now
            </button>
            <DispatchGroupChoice state={state} value={group} busy={blocked} onChange={setGroup} />
            <DispatchPostureChoice
              state={state}
              value={posture}
              busy={blocked}
              onChange={setPosture}
            />
            <DispatchRunnerNote state={state} user={user} group={group} posture={posture} />
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

/**
 * Which class of runner to spend this one dispatch on.
 *
 * A `<select>` over groups this machine already defines, never a text field, for the
 * same reason the runner control on the settings page is one: the browser may choose
 * among things a human wrote into `dispatch.yaml`, and may never describe a new one.
 *
 * **Absent entirely on a machine that defines no groups.** A pulldown with one option
 * meaning "the only thing that can happen" is furniture, and a project that never had
 * a group must go on reading exactly as it did before groups existed.
 *
 * This is the shape task-307 copies for posture: a labelled select whose empty option
 * is the project's own answer, spelled out, and whose value is sent only when it is not
 * that. Nothing about it is specific to groups except the list and the two words.
 */
function DispatchGroupChoice({
  state,
  value,
  busy,
  onChange,
}: {
  state: DispatchStateView | null;
  value: string;
  busy: boolean;
  onChange: (next: string) => void;
}) {
  const groups = state?.available_groups ?? [];
  if (groups.length === 0) return null;
  return (
    <div className="flex items-center gap-2">
      <label htmlFor="dispatch-group" className="text-sm text-dark-muted">
        Group
      </label>
      <select
        id="dispatch-group"
        value={value}
        disabled={busy}
        onChange={(event) => onChange(event.target.value)}
        className="rounded-lg border border-dark-border bg-dark-bg p-2 text-sm text-dark-text focus:border-sky-500 focus:outline-none"
      >
        <option value="">{projectDefaultLabel(state)}</option>
        {groups.map((name) => (
          <option key={name} value={name}>
            {name}
          </option>
        ))}
      </select>
    </div>
  );
}

/**
 * What this one run may do, chosen at the moment of dispatching (task-307).
 *
 * The same shape as `DispatchGroupChoice` above, deliberately: a labelled select whose
 * empty option is the project's own answer, whose value is sent only when it is not
 * that, and which is absent entirely when there is nothing to choose between.
 *
 * Three things about it are not cosmetic.
 *
 * **The list comes from the server.** `offerable_postures` is every posture at or below
 * the project's machine-local ceiling, and a browser that derived its own list from the
 * enum would be the one place in the system that could offer a choice the dispatch API
 * refuses -- which teaches the operator that the control lies. The refusal still exists
 * server-side (`posture_above_ceiling`); this just makes it unreachable by clicking.
 *
 * **Each option says what it does to the branch, not what it is called.** "autonomous"
 * tells a reader nothing about whether their work merges without them, and after
 * task-021 that is exactly what it decides. The consequence text is keyed off
 * `posture_merge_policies`, which the server sends for the same reason as the list.
 *
 * **`autonomous` is offered disabled when this project has no scripted finish**, rather
 * than silently dropped. task-021 accepted that an autonomous merge runs through
 * `agentjobs finish --posture-release` and that a machine without it has no sanctioned
 * mechanism for one; picking it there would produce a run told it may merge with no way
 * to. Disabled-with-a-reason is right where omitting it is wrong, because the fix is one
 * line of the reader's own config and they can only make it if they know it is the cause.
 */
function DispatchPostureChoice({
  state,
  value,
  busy,
  onChange,
}: {
  state: DispatchStateView | null;
  value: string;
  busy: boolean;
  onChange: (next: string) => void;
}) {
  const postures = state?.offerable_postures ?? [];
  // One option means there is nothing to choose and a pulldown saying so is furniture --
  // the same rule the group select uses. Note this is *not* "the project has not raised
  // its ceiling": an unraised ceiling is the project's own posture, so a project at
  // `auto` still offers the three postures at or below it and simply cannot escalate.
  // The only ceiling with nothing under it is `read_only`.
  if (postures.length <= 1) return null;
  return (
    <div className="flex items-center gap-2">
      <label htmlFor="dispatch-posture" className="text-sm text-dark-muted">
        Envelope
      </label>
      <select
        id="dispatch-posture"
        value={value}
        disabled={busy}
        onChange={(event) => onChange(event.target.value)}
        className="rounded-lg border border-dark-border bg-dark-bg p-2 text-sm text-dark-text focus:border-sky-500 focus:outline-none"
      >
        <option value="">Project default</option>
        {postures.map((name) => {
          const blocked = postureNeedsFinish(name, state);
          const says = blocked ? FINISH_REQUIRED : mergeConsequence(name, state);
          return (
            <option key={name} value={name} disabled={blocked}>
              {says ? `${name} — ${says}` : name}
            </option>
          );
        })}
      </select>
    </div>
  );
}

/** Why `autonomous` cannot be picked on a project that has not switched the finish on. */
const FINISH_REQUIRED = "needs finish.enabled on this project";

/**
 * Whether choosing this posture would grant a merge the project cannot actually perform.
 *
 * Only ever true of a posture whose merge policy is `automatic`, which is the only one
 * that runs through the scripted finish. Everything else is unaffected by the switch.
 */
function postureNeedsFinish(posture: string, state: DispatchStateView | null): boolean {
  return state?.posture_merge_policies?.[posture] === "automatic" && !state?.finish_enabled;
}

/**
 * What this posture does to the branch, in the operator's terms rather than the enum's.
 *
 * Null when the server named a posture but not its policy, and the callers render
 * nothing at all in that case. Saying something vague would be worse than saying
 * nothing: this sentence is the one a reader decides on, so filler in the slot where
 * "merges without you" belongs is actively misleading.
 */
function mergeConsequence(posture: string, state: DispatchStateView | null): string | null {
  switch (state?.posture_merge_policies?.[posture]) {
    case "automatic":
      return "merges its own work when the gate passes, no review";
    case "review":
      return "stops for your review before merging";
    case "none":
      return "no shell, nothing to merge";
    default:
      return null;
  }
}

/** What the run will be, and whose name goes on it. Both worth reading before clicking. */
function DispatchRunnerNote({
  state,
  user,
  group,
  posture: chosen,
}: {
  state: DispatchStateView | null;
  user: string;
  group: string;
  posture: string;
}) {
  // The chosen posture outranks the project's, so the sentence has to name what will
  // actually run rather than what the config says -- the same reason the group branch
  // below names the overriding group. Saying "posture auto" beside a pulldown reading
  // `autonomous` is the one sentence here that would be reliably wrong.
  const effective = chosen || state?.posture;
  const consequence = effective ? mergeConsequence(effective, state) : null;
  const posture = (
    <>
      posture <strong className="text-dark-text">{effective}</strong>
      {consequence ? <> — {consequence}</> : null}
      {/* Push is per project and never the posture's (task-021). Named only when it is
          on, because `false` is the answer everywhere today and a sentence repeating
          the universal default on every task is noise. */}
      {state?.push ? <>, and pushes</> : null}, authorised by{" "}
      <strong className="text-dark-text">{user}</strong>
    </>
  );
  // A group picked for this one dispatch outranks everything the project says, and the
  // browser holds no member list to resolve it with -- so it names the group that will
  // choose rather than guessing which member wins. Leaving the project's runner on
  // screen beside an overriding group would be the one sentence here that is reliably
  // wrong.
  if (group) {
    return (
      <span className="text-sm text-dark-muted">
        Runner chosen from group <strong className="text-dark-text">{group}</strong>, {posture}
      </span>
    );
  }
  // `resolved_runner` first: a project pointed at a group has no `runner` of its own,
  // and reading only that field is why a fully configured project used to say nothing
  // here at all.
  const runner = state?.resolved_runner ?? state?.runner ?? null;
  if (!runner) return null;
  return (
    <span className="text-sm text-dark-muted">
      Runner <strong className="text-dark-text">{runner}</strong>
      {state?.resolved_group ? (
        <>
          {" "}
          from group <strong className="text-dark-text">{state.resolved_group}</strong>
        </>
      ) : null}
      , {posture}
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

/**
 * What a project is being pointed at when it is enabled: one existing name, one kind.
 *
 * Never both. A group already says which runners are in play, so naming one of them
 * beside it says two things at once -- the API refuses that outright, and this type is
 * the browser half of the same rule.
 */
export type DispatchEnableTarget = { runner: string | null } | { group: string };

export type DispatchSettingsProps = {
  state: DispatchStateView | null;
  busy?: boolean;
  error?: string | null;
  onEnable: (target: DispatchEnableTarget) => Promise<void> | void;
  onDisable: () => Promise<void> | void;
};

/**
 * What this project's runs resolve to, for the gate tile that used to only know runners.
 *
 * The order is the config's own precedence ladder, and it is read off the server's
 * answer rather than re-derived: a resolved group wins, then whatever the project names
 * but a shut gate stopped us resolving, then a plain runner, then the machine-wide
 * default group. Only when none of those exist is there genuinely no runner chosen --
 * which is why a project configured with a group no longer says so (task-184).
 */
function projectGateDetail(state: DispatchStateView): string {
  if (state.resolved_group) {
    const via = state.resolved_from === "machine" ? " (machine default)" : "";
    return state.resolved_runner
      ? `group: ${state.resolved_group}${via} → ${state.resolved_runner}`
      : `group: ${state.resolved_group}${via}`;
  }
  if (state.group) return `group: ${state.group}`;
  if (state.runner) return `runner: ${state.runner}`;
  if (state.default_group) return `group: ${state.default_group} (machine default)`;
  return "no runner chosen";
}

/**
 * One entry in the enable control: what the browser shows, and what it would post.
 *
 * The target travels with the option rather than being parsed back out of the selected
 * value. Parsing would need the value to encode a kind, which would change what a plain
 * runner's `<option value>` has always been on a machine with no groups -- and that is
 * exactly the thing sc-4 says must not move. Carrying it costs one field and removes
 * the question entirely.
 */
type EnableOption = { value: string; label: string; target: DispatchEnableTarget };

/** A group's value, kept distinct from a runner's, which stays the bare name. */
function groupOptionValue(name: string): string {
  return `group:${name}`;
}

/**
 * The per-project switch, and the machine state it depends on.
 *
 * Disable is rendered unconditionally whenever the project is on: no confirmation, no
 * second click, no explanation demanded first. A kill switch you cannot reach is not
 * one, and one that argues with you is worse than none.
 *
 * The runner is a `<select>` over names this machine defines, never a text field. A
 * free-text runner would let a browser name a command, which is precisely the thing the
 * whole gate design exists to prevent -- and the API would refuse it anyway. The same
 * list carries this machine's runner *groups*, because pointing a project at an
 * existing group is the identical act on identical terms: choosing among definitions a
 * human wrote by hand, never authoring one.
 */
export function DispatchSettings({
  state,
  busy = false,
  error = null,
  onEnable,
  onDisable,
}: DispatchSettingsProps) {
  const [target, setTarget] = useState<string>("");
  if (!state) {
    return <p className="text-dark-muted">Reading this machine's dispatch configuration…</p>;
  }

  const runners = state.available_runners ?? [];
  const groups = state.available_groups ?? [];
  // One list rather than two controls, because the config makes it one choice: a
  // project resolves through a group or through a runner and never both, and two
  // controls could be set to say otherwise. The kind travels in the value.
  //
  // On a machine with no groups the options are bare runner names, exactly as they were
  // before groups existed -- a "runner: " prefix on a list that can only hold runners
  // is noise a project that never had a group should not have to read.
  const options: Array<EnableOption> =
    groups.length === 0
      ? runners.map((name) => ({ value: name, label: name, target: { runner: name } }))
      : [
          ...groups.map((name) => ({
            value: groupOptionValue(name),
            label: `group: ${name}`,
            target: { group: name },
          })),
          ...runners.map((name) => ({
            value: name,
            label: `runner: ${name}`,
            target: { runner: name },
          })),
        ];
  // Preselect what the project is already pointed at, group included. Until task-184
  // this fell straight through to `runners[0]` for a grouped project, so the control
  // offered to change something it would then silently decline to change: the config
  // layer keeps a project's `group:` and ignores a `runner:` sent beside it, verified
  // against a throwaway home, so pressing Enable did nothing and said nothing.
  const current = state.group ? groupOptionValue(state.group) : (state.runner ?? "");
  const chosen = target || current || options[0]?.value || "";
  const selected = options.find((option) => option.value === chosen);

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
            detail={projectGateDetail(state)}
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
                  {groups.length === 0 ? "Runner" : "Runner or group"}
                </label>
                <select
                  id="dispatch-runner"
                  value={chosen}
                  disabled={busy || options.length === 0}
                  onChange={(event) => setTarget(event.target.value)}
                  className="mt-1 rounded-lg border border-dark-border bg-dark-bg p-2 text-dark-text"
                >
                  {options.length === 0 && <option value="">none defined</option>}
                  {options.map((option) => (
                    <option key={option.value} value={option.value}>
                      {option.label}
                    </option>
                  ))}
                </select>
              </div>
              <button
                type="button"
                disabled={busy || options.length === 0}
                onClick={() => void onEnable(selected?.target ?? { runner: chosen || null })}
                className="touch-target rounded-lg bg-emerald-600 px-4 font-semibold text-white hover:bg-emerald-700 disabled:opacity-60"
              >
                Enable dispatch for this project
              </button>
            </>
          )}
        </div>

        <p className="mt-4 text-xs text-dark-muted">
          {groups.length === 0 ? "Runners are" : "Runners and runner groups are"} defined
          by hand in <code>{state.config_path}</code> and never from this page. This
          switch chooses among commands that already exist on this machine; it cannot
          describe a new one.
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
