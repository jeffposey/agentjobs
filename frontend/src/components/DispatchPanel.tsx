import { useEffect, useRef, useState, type ReactNode } from "react";

import type { ReviewIdentity } from "../api/generated";
import type {
  DispatchRunView,
  DispatchStateView,
  MergeMode,
  QueuedDispatchState,
} from "../api/types";
import { DictationControl, DictationNote } from "./DictationControl";

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
    "Dispatch is stopped machine-wide by the emergency stop. Press Stopped in the header to resume it.",
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
  task_being_worked:
    "An agent this machine did not start is holding this task. AgentJobs cannot see it or stop it — it ends when that agent hands off, releases or closes the task. If it is gone, release the task and dispatch a fresh one.",
  concurrency_limit:
    "The machine is full. Choose below: wait for the next free slot, start anyway above the ceiling, or leave it — or cancel one of the runs named above.",
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
  // The remedy is the three-way prompt this panel renders under the refusal (task-461).
  // The server's sentence is read by the CLI and by MCP, where the remedy is a flag
  // rather than a button, so it cannot name the buttons and this one has to.
  "concurrency_limit",
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
 * per-dispatch choice arrives in: task-307 adds `merge_mode` here and changes nothing else
 * about how a dispatch is started.
 *
 * Every field is optional, and an omitted one means "whatever the project resolves to".
 * The browser never restates a default it did not choose, so a dispatch with nothing
 * picked posts exactly the body it posted before this control existed -- which is what
 * keeps a machine with no groups behaving as it did before they were added.
 */
export type DispatchOptions = {
  /** Specific runner for this run. Mutually exclusive with group. */
  runner?: string;
  /** Runner group to choose from. Outranks the project's own group and its runner. */
  group?: string;
  /**
   * `review` or `automerge` for this one run. Outranks the task record's own and the
   * project's default, and automerge is refused where the project does not allow it
   * (task-307, task-308, task-602).
   */
  merge_mode?: MergeMode;
  /** The human's brief, sent only when the panel asked for one. */
  note?: string;
  /**
   * What to do if every slot is taken: refuse (the default, and the server's) or wait
   * for one (task-459).
   *
   * Sent only when somebody pressed the button that means it, so an ordinary Dispatch
   * posts the body it always posted. The three-way prompt before the first click is
   * task-461; until it lands this is offered where the refusal already appears, which
   * is the one moment the answer is certainly relevant.
   */
  if_full?: "refuse" | "queue";
  /**
   * Start now, above `limits.max_concurrent_runs` (task-461).
   *
   * The other answer to a full machine, and the opposite one: `if_full: queue` waits
   * for a slot and this takes none. Sent only from the prompt's *Dispatch now*, and the
   * server refuses it for anything but a human principal — the button is the offer, not
   * the check.
   */
  over_ceiling?: boolean;
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
 *
 * Exported since task-152, because the Run checks button meets the *same gate*: running
 * a check starts a process of the record's choosing, so `POST .../check` goes through
 * `assert_dispatch_permitted` and comes back under these same reason codes. The server
 * renders those refusals once for exactly this reason -- `dispatch_refusal_error` is
 * shared between the dispatch route and the check route -- and two renderings on this
 * side would be the same mistake, one screen lower.
 */
export function RefusalNote({
  refusal,
  answered = true,
  children,
}: {
  refusal: DispatchRefusal;
  answered?: boolean;
  /** A control that acts on this refusal, when the page can offer one. */
  children?: React.ReactNode;
}) {
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
      {children && <div className="mt-3">{children}</div>}
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
   * Who the task record says is working this task, and since when (task-179).
   *
   * Non-null does not on its own mean the button is withheld: a claim with a run behind
   * it is a run, and `liveRun` above already speaks for those. What this catches is a
   * claim with *no run at all* — an agent AgentJobs did not start, which is the only
   * kind that writes a claim and nothing else. See `unseenAgent` below.
   */
  heldByAgent: { owner: string; since: string } | null;
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
  /**
   * An epic walk is supervising this task (task-591).
   *
   * Disables the button and says so, the way `finishLive` does. The server would take the
   * click as a second walk and `_Supervision.open` would turn it away as
   * `already_supervised`: a no-op run on the record, and nothing started. The walk card
   * directly above says what it is doing instead.
   */
  walkOpen?: boolean;
  cancellingRunId?: string | null;
  /**
   * What happened to the last click when it did not start a run but was not refused
   * either: a dispatch accepted into the machine's queue (task-459).
   *
   * Its own prop rather than a flavour of refusal, because it is the opposite of one.
   * Rendering "queued, place 2" inside an orange refusal box would tell somebody their
   * dispatch failed at the moment it succeeded.
   */
  queuedNotice?: string | null;
  /**
   * The dispatch of this task that is already waiting for a free slot (task-476).
   *
   * Read off the task record rather than from the slot board, so the panel and the
   * page's own status chip come from one answer. It is the standing fact -- a dispatch
   * is waiting, whoever queued it and whenever -- where `queuedNotice` is what *this*
   * click did; the notice goes when the page is reloaded and this does not.
   */
  queuedDispatch?: QueuedDispatchState | null;
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
  heldByAgent,
  identity,
  recordCanBrief,
  busy = false,
  finishLive = false,
  walkOpen = false,
  cancellingRunId = null,
  dispatchRefusal = null,
  queuedNotice = null,
  queuedDispatch = null,
  onDispatch,
  onCancel,
  renderOutput,
}: DispatchPanelProps) {
  const [brief, setBrief] = useState("");
  const briefRef = useRef<HTMLTextAreaElement>(null);
  // Empty means "the project's own", and it is deliberately not pre-filled with the
  // project's group: a value the human did not pick must not be sent as though they
  // had. Reset per page load rather than remembered -- "audit this one with the big
  // model" is a decision about one task, and a sticky override would silently apply it
  // to the next one.
  const [group, setGroup] = useState("");
  const [runner, setRunner] = useState("");
  // Same rule as `group`, and it matters more here: an override that stuck would carry
  // "let this one merge itself" onto the next task the reader opened, which is the one
  // sticky default nobody would want.
  const [mergeMode, setMergeMode] = useState("");
  // The three-way prompt a full machine gets instead of a refusal (task-461). Two
  // pieces of state rather than one because the prompt has two sources: the reader
  // pressed Dispatch on a machine the state endpoint already said was full, or a click
  // came back `concurrency_limit` because it filled in between. Cancel has to close it
  // from either, and only `dismissed` can close the second.
  const [promptOpen, setPromptOpen] = useState(false);
  const [promptDismissed, setPromptDismissed] = useState(false);
  // A *new* refusal re-opens a prompt the reader dismissed. Dismissing is an answer to
  // the question in front of them, not a standing instruction to stop asking: the next
  // click is a new question and deserves the choice again.
  useEffect(() => {
    setPromptDismissed(false);
  }, [dispatchRefusal]);

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
  // A run for this task is going, so the server would refuse this click with
  // `live_run_exists` (task-354). Withheld rather than pressable-into-a-refusal, for
  // the same reason `finishLive` is: the reader can see what holds it, right above.
  const liveRun = runs.find((run) => run.live) ?? null;
  // A dispatch of this task is already waiting for a slot, so the server would refuse a
  // second one with `already_queued` (task-476). Withheld for the same reason `liveRun`
  // is: the answer the button would get back is already known, and what the reader
  // actually wants at this point is the entry and a way to call it off.
  // The record says an agent is working this task and this machine has no run for it at
  // all — not a live one and not a finished one (task-179). That pair is an agent
  // AgentJobs did not start: a session from the spawn-session skill, a person in a
  // terminal, another tool. The server refuses the click with `task_being_worked`, and
  // the same two facts are readable here, so the page says so before the press.
  //
  // `runs.length === 0` rather than `!liveRun`, and the difference is the whole rule: a
  // *finished* run for this task is AgentJobs saying it started an agent here and
  // watched it end, which accounts for a claim left behind by a process that is gone.
  // With no run ever, nothing accounts for the claim and the claim is all there is.
  //
  // Not while a walk is open: the walk's own claim is what `heldByAgent` would be reading,
  // and the walk note below accounts for it.
  const unseenAgent = !walkOpen && heldByAgent && runs.length === 0 ? heldByAgent : null;
  const offerButton =
    taskIsDispatchable &&
    Boolean(state?.can_dispatch) &&
    !liveRun &&
    !queuedDispatch &&
    !unseenAgent;
  const user = identity.ok ? identity.user : null;
  // The special occasion, from either direction: the record looks insufficient here, or
  // the server said so when the button was pressed. Honouring the server's answer as
  // well as the local one means the box still appears if the two ever drift apart.
  const askForBrief = !recordCanBrief || dispatchRefusal?.reason === "insufficient_record";
  // One name for "the button must not be pressable", so the plain button and the
  // brief form cannot drift apart on what disables them.
  const blocked = busy || finishLive || walkOpen;
  // What the state endpoint says about the machine, which is a different question from
  // `can_dispatch` (the four configuration gates). Full is a question to ask, not a
  // reason to withhold the button — see `FullMachinePrompt`.
  const machineFull = Boolean(state?.machine_full);
  const refusedFull = dispatchRefusal?.reason === "concurrency_limit";
  const showFullPrompt = !promptDismissed && (promptOpen || refusedFull);
  /** What this click will send. Omitted keys are the point -- see `DispatchOptions`. */
  const options = (note?: string): DispatchOptions => ({
    ...(runner ? { runner } : {}),
    ...(group ? { group } : {}),
    // Narrowed rather than validated: every value this can hold came out of
    // `offerable_merge_modes`, which the server derives from the same `MergeMode` enum
    // the generated type is generated from. A mode the API would reject cannot reach
    // here, and if one ever did the API refuses it under `automerge_not_allowed`.
    ...(mergeMode ? { merge_mode: mergeMode as MergeMode } : {}),
    ...(note ? { note } : {}),
  });

  return (
    <section
      className="space-y-4 rounded-xl border-2 border-sky-700/50 bg-sky-950/30 p-4 @min-[768px]:p-6"
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

      {taskIsDispatchable && liveRun && (
        // The state task-354 was filed for: a task somebody is working, which used to
        // show a Dispatch button offering to start a second agent on the same task and
        // the same repository. Status rather than alert -- it describes the world, not
        // an act the reader just took.
        <div
          role="status"
          data-refusal-reason="live_run_exists"
          className="rounded-lg border border-sky-600/50 bg-sky-950/40 p-3 text-sm text-sky-100"
        >
          <p>
            {liveRun.mode === "interactive"
              ? "A session is working this task right now."
              : "An agent is already running on this task."}{" "}
            One run per task, so there is nothing to start.
          </p>
          <p className="mt-2 text-sky-200">
            {liveRun.mode === "interactive"
              ? "It is a chat session rather than a dispatched agent, so AgentJobs did not start it and will not stop it. It ends when the task is handed off, released or closed."
              : "Watch it below. Cancel it there if you want to start a different one."}
          </p>
        </div>
      )}

      {taskIsDispatchable && unseenAgent && (
        // Task-179's state: the record's claim is the only thing that knows this agent
        // exists. Status rather than alert, for the same reason the live-run box above
        // is one — it describes the world rather than answering an act the reader took.
        <div
          role="status"
          data-refusal-reason="task_being_worked"
          className="rounded-lg border border-sky-600/50 bg-sky-950/40 p-3 text-sm text-sky-100"
        >
          <p>
            <strong>{unseenAgent.owner}</strong> claimed this task on{" "}
            {new Date(unseenAgent.since).toLocaleString()} and nothing has moved the ball
            since. One agent per task, so there is nothing to start.
          </p>
          <p className="mt-2 text-sky-200">
            AgentJobs did not start it and has no run for it, so it cannot show you the
            agent or stop it. It ends when that agent hands the task off, releases it, or
            closes it. If it is gone, release the task — that records that somebody
            decided so — and dispatch a fresh one.
          </p>
        </div>
      )}

      {taskIsDispatchable && !liveRun && queuedDispatch && (
        <QueuedEntryNote
          entry={queuedDispatch}
          busy={cancellingRunId === queuedDispatch.queue_id}
          onCancel={() => onCancel(queuedDispatch.queue_id)}
        />
      )}

      {offerButton && walkOpen && (
        // Status rather than alert, for the reason the finish note below gives.
        <div
          role="status"
          data-refusal-reason="already_supervised"
          className="rounded-lg border border-sky-600/50 bg-sky-950/40 p-3 text-sm text-sky-100"
        >
          <p>
            An epic walk is supervising this task and starts its children itself, so there
            is nothing to dispatch.
          </p>
          <p className="mt-2 text-sky-200">
            Follow it above. This button comes back when the walk ends.
          </p>
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
            onClick={() => {
              // Asked rather than sent, when the machine is already known to be full
              // (task-461). The round trip this saves is not the point; the point is
              // that the answer is a decision and the reader is here to make it.
              if (machineFull) {
                setPromptDismissed(false);
                setPromptOpen(true);
                return;
              }
              void onDispatch(options());
            }}
            className="touch-target rounded-lg bg-sky-600 px-4 font-semibold text-white hover:bg-sky-500 disabled:opacity-60"
          >
            ▶ Dispatch — start an agent now
          </button>
          <DispatchTargetChoice
            state={state}
            runner={runner}
            group={group}
            busy={blocked}
            onChange={(next) => {
              setRunner(next.runner);
              setGroup(next.group);
            }}
          />
          <MergeModeChoice
            id="dispatch-merge-mode"
            state={state}
            value={mergeMode}
            busy={blocked}
            onChange={setMergeMode}
          />
          <DispatchRunnerNote
            state={state}
            user={user}
            runner={runner}
            group={group}
            mergeMode={mergeMode}
          />
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
          {/* `relative` so the microphone can position itself into the right-hand end
              of the label line. The dispatch prompt task-172 names: the always-present
              optional instruction box is task-162's and does not exist yet, so this is
              the one place a person writes a brief for a run today. */}
          <div className="relative">
            <label htmlFor="dispatch-brief" className="block text-sm font-semibold">
              This task has no specification — say what the agent should do
            </label>
            <textarea
              ref={briefRef}
              id="dispatch-brief"
              required
              rows={4}
              value={brief}
              onChange={(event) => setBrief(event.target.value)}
              placeholder="What the agent should do…"
              className="w-full rounded-lg border border-dark-border bg-dark-bg p-3 text-dark-text focus:border-sky-500 focus:outline-none"
            />
            <DictationControl
              label="What the agent should do"
              target={() => briefRef.current}
            />
            <DictationNote />
          </div>
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
            <DispatchTargetChoice
              state={state}
              runner={runner}
              group={group}
              busy={blocked}
              onChange={(next) => {
                setRunner(next.runner);
                setGroup(next.group);
              }}
            />
            <MergeModeChoice
              id="dispatch-merge-mode"
              state={state}
              value={mergeMode}
              busy={blocked}
              onChange={setMergeMode}
            />
            <DispatchRunnerNote
              state={state}
              user={user}
              runner={runner}
              group={group}
              mergeMode={mergeMode}
            />
          </div>
        </form>
      )}

      {queuedNotice && (
        <p
          role="status"
          data-testid="dispatch-queued-notice"
          className="rounded-lg border border-amber-600/50 bg-amber-950/30 p-3 text-sm text-amber-100"
        >
          {queuedNotice}
        </p>
      )}
      {taskIsDispatchable && gateRefusal && <RefusalNote refusal={gateRefusal} answered={false} />}
      {dispatchRefusal && !refusedFull && <RefusalNote refusal={dispatchRefusal} />}
      {/* A full machine is rendered as the prompt rather than as a refusal, whichever
          way the panel learned of it. Orange-boxing it would announce a failure to a
          question that has three good answers and has not been asked yet. The refusal's
          own sentence rides inside the prompt, because it is the sentence that names
          the runs. */}
      {showFullPrompt && (
        <FullMachinePrompt
          // The state endpoint's sentence where there is one — it is the current
          // answer, and `onDispatch` re-reads it. The refusal's own sentence is the
          // fallback: it names the same runs, in the same words, as of the click.
          holders={state?.slot_holders || (refusedFull ? dispatchRefusal.message : "")}
          occupied={state?.machine_occupied ?? 0}
          ceiling={state?.machine_ceiling ?? 0}
          busy={blocked}
          onQueue={() => {
            setPromptOpen(false);
            setPromptDismissed(true);
            void onDispatch({ ...options(), if_full: "queue" });
          }}
          onDispatchNow={() => {
            setPromptOpen(false);
            setPromptDismissed(true);
            void onDispatch({ ...options(), over_ceiling: true });
          }}
          onCancel={() => {
            // Nothing is sent, nothing is written, and the panel goes back to resting.
            setPromptOpen(false);
            setPromptDismissed(true);
          }}
        />
      )}

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
 * A dispatch of this task waiting for a free slot, and the one act left to take on it.
 *
 * The state task-476 was filed for, from the other end: the task page used to read
 * `Ready` and offer to start an agent, while the machine had already promised to start
 * one. Two dispatches on one task is the thing the queue's own `already_queued` rule
 * exists to prevent, so the button that would ask for a second one is replaced by the
 * button that calls the first one off.
 *
 * `role="status"` rather than `alert`: it describes the world, not something the reader
 * just did. The same distinction the live-run note draws, and for the same reason -- a
 * screen reader must not interrupt on every poll.
 *
 * Cancelling goes through `onCancel` with the *queue* id, which is not a mistake: a
 * queued entry cancels through the same route as the run it has not become, because it
 * is the same act from where the person is standing.
 */
function QueuedEntryNote({
  entry,
  busy,
  onCancel,
}: {
  entry: QueuedDispatchState;
  busy: boolean;
  onCancel: () => void;
}) {
  const paused = Boolean(entry.paused_by);
  const starting = entry.status === "starting";
  return (
    <div
      role="status"
      data-refusal-reason="already_queued"
      data-testid="dispatch-queued-entry"
      data-queue-id={entry.queue_id}
      data-queue-position={entry.position}
      className="rounded-lg border border-amber-600/50 bg-amber-950/30 p-3 text-sm text-amber-100"
    >
      <p>
        {starting
          ? "A dispatch of this task is starting now — it is going through the dispatch gates."
          : entry.position > 1
            ? `A dispatch of this task is waiting for a free slot, place ${entry.position} in line.`
            : "A dispatch of this task is waiting for the next free slot."}{" "}
        Nothing has started yet.
      </p>
      <p className="mt-2 text-amber-200">
        {paused
          ? `Nothing is being tried on this credential while incident ${entry.paused_by} is open, and the entry keeps its place in line.`
          : "Every dispatch gate is judged when it starts, not when it was queued, so a gate that refuses then writes onto this task."}
      </p>
      {/* No second Dispatch button, and deliberately no "dispatch anyway": the server
          answers `already_queued` to that, and offering a click whose only outcome is a
          refusal is what this note replaced. */}
      <div className="mobile-action-row mt-3 flex flex-wrap items-center gap-3">
        <button
          type="button"
          disabled={busy || starting}
          data-testid="dispatch-cancel-queued"
          onClick={onCancel}
          className="touch-target rounded-lg border border-amber-500/60 bg-amber-900/40 px-3 text-sm font-semibold text-amber-100 hover:bg-amber-900/60 disabled:opacity-60"
        >
          {busy ? "Cancelling…" : "Cancel the queued dispatch"}
        </button>
        {starting && (
          // It is past the point where removing it from the queue does anything: the
          // tick has claimed it, and what there is to stop in a second's time is a run,
          // under a different id, in the list below.
          <span className="text-xs text-amber-200">
            Too late to take it out of the queue. If it starts, it appears below as a run.
          </span>
        )}
      </div>
    </div>
  );
}

/**
 * The three-way choice a full machine gets, instead of a refusal (task-461).
 *
 * The ceiling stops a *click* starting an agent the machine cannot afford. That is a
 * good default and a bad final answer: the person pressing Dispatch knows things
 * `max_concurrent_runs` does not — that two of the three runs are stalled on a review,
 * that this one is the five-minute job unblocking the others. So the panel asks
 * instead of refusing, with the runs holding the slots named, and the three answers
 * are the three things a person actually wants:
 *
 * - **Queue** — wait for a slot. `if_full: queue`, task-459's durable entry.
 * - **Dispatch now** — take one anyway. `over_ceiling: true`, refused by the server for
 *   anything but a human principal; this button is the offer, never the check.
 * - **Cancel** — nothing happens, and nothing is written anywhere.
 *
 * **A prompt, not a modal.** It does not trap focus, it does not cover the page, and
 * Escape is Cancel. What is behind it — the runs list, the task, the review panel — is
 * exactly what somebody deciding between these three needs to be able to read.
 */
function FullMachinePrompt({
  holders,
  occupied,
  ceiling,
  busy,
  onQueue,
  onDispatchNow,
  onCancel,
}: {
  /** The runs holding the slots, in the server's own sentence. */
  holders: string;
  occupied: number;
  ceiling: number;
  busy: boolean;
  onQueue: () => void;
  onDispatchNow: () => void;
  onCancel: () => void;
}) {
  // The numbers are quoted only when they agree with the sentence above them. The
  // prompt can be raised by a refusal that arrived before the state endpoint was
  // re-read, and "every slot is taken — 0 of 1" is a sentence that argues with itself.
  const counted = ceiling > 0 && occupied >= ceiling;
  return (
    <div
      role="group"
      aria-label="The machine is full"
      data-testid="dispatch-full-prompt"
      // Escape is Cancel. On the container rather than on each button so it works
      // wherever focus happens to be inside the prompt, and `onKeyDown` rather than a
      // document listener so it cannot swallow Escape from anything else on the page.
      onKeyDown={(event) => {
        if (event.key === "Escape") {
          event.stopPropagation();
          onCancel();
        }
      }}
      className="space-y-3 rounded-lg border border-amber-600/50 bg-amber-950/30 p-3 text-sm text-amber-100"
    >
      <p className="font-semibold">
        Every slot on this machine is taken{counted ? ` — ${occupied} of ${ceiling}` : ""}.
      </p>
      {/* Named, not counted. This panel's own run list shows only this task's runs, so
          the run standing in the way is by definition one this page cannot draw. */}
      {holders && <p className="text-amber-200">{holders}</p>}
      <div className="mobile-action-row flex flex-wrap items-center gap-3">
        <button
          type="button"
          disabled={busy}
          data-testid="dispatch-queue-it"
          onClick={onQueue}
          className="touch-target rounded-lg bg-amber-600 px-4 font-semibold text-white hover:bg-amber-500 disabled:opacity-60"
        >
          Queue it for the next free slot
        </button>
        <button
          type="button"
          disabled={busy}
          data-testid="dispatch-over-ceiling"
          onClick={onDispatchNow}
          className="touch-target rounded-lg border border-amber-500/60 bg-amber-900/40 px-3 text-sm font-semibold text-amber-100 hover:bg-amber-900/60 disabled:opacity-60"
        >
          Dispatch now — above the ceiling
        </button>
        <button
          type="button"
          data-testid="dispatch-full-cancel"
          onClick={onCancel}
          className="touch-target rounded-lg px-3 text-sm font-semibold text-amber-200 underline hover:text-amber-100"
        >
          Cancel
        </button>
      </div>
      <p className="text-amber-200">
        Queueing starts it on its own when a slot frees, with every dispatch gate judged
        at that moment. Dispatching now runs{" "}
        {counted ? `${occupied + 1} agents against a ceiling of ${ceiling}` : "one more agent than the ceiling allows"}{" "}
        until one of them ends — every other limit still applies, including the hourly
        cap. Cancel writes nothing.
      </p>
    </div>
  );
}

/** One unambiguous choice between the project default, a group, and a specific model. */
function DispatchTargetChoice({
  state,
  runner,
  group,
  busy,
  onChange,
}: {
  state: DispatchStateView | null;
  runner: string;
  group: string;
  busy: boolean;
  onChange: (next: { runner: string; group: string }) => void;
}) {
  const runners = state?.available_runners ?? [];
  const groups = state?.available_groups ?? [];
  if (runners.length === 0 && groups.length === 0) return null;
  const value = runner ? runnerOptionValue(runner) : group ? groupOptionValue(group) : "";

  const choose = (next: string) => {
    if (next.startsWith("runner:")) {
      onChange({ runner: next.slice("runner:".length), group: "" });
    } else if (next.startsWith("group:")) {
      onChange({ runner: "", group: next.slice("group:".length) });
    } else {
      onChange({ runner: "", group: "" });
    }
  };

  return (
    <div className="flex items-center gap-2">
      <label htmlFor="dispatch-target" className="text-sm text-dark-muted">
        Run with
      </label>
      <select
        id="dispatch-target"
        value={value}
        disabled={busy}
        onChange={(event) => choose(event.target.value)}
        className="min-w-52 rounded-lg border border-dark-border bg-dark-bg p-2 text-sm text-dark-text focus:border-sky-500 focus:outline-none"
      >
        <option value="">Project default</option>
        {groups.length > 0 && (
          <optgroup label="Automatic groups">
            {groups.map((name) => (
              <option key={name} value={groupOptionValue(name)}>
                {humanizeChoiceName(name)}
              </option>
            ))}
          </optgroup>
        )}
        <optgroup label="Specific agents">
          {runners.map((name) => (
            <option key={name} value={runnerOptionValue(name)}>
              {runnerLabel(state, name)}
            </option>
          ))}
        </optgroup>
      </select>
    </div>
  );
}

function runnerOptionValue(name: string): string {
  return `runner:${name}`;
}

function runnerLabel(state: DispatchStateView | null, name: string): string {
  return state?.runner_labels?.[name] ?? name;
}

function humanizeChoiceName(name: string): string {
  return name
    .split(/[-_]/)
    .filter(Boolean)
    .map((word) => word.slice(0, 1).toUpperCase() + word.slice(1))
    .join(" ");
}

/**
 * Whether this one run stops for review or merges itself, chosen at dispatch (task-307).
 *
 * The same shape as `DispatchTargetChoice` above, deliberately: a labelled select whose
 * empty option is the project's own answer, whose value is sent only when it is not
 * that, and which is absent entirely when there is nothing to choose between.
 *
 * Three things about it are not cosmetic.
 *
 * **The list comes from the server.** `offerable_merge_modes` is `review`, plus
 * `automerge` where this machine's `dispatch.yaml` allows it. A browser that derived its
 * own list would be the one place in the system that could offer a choice the dispatch
 * API refuses (`automerge_not_allowed`); this just makes that unreachable by clicking.
 *
 * **Each option says what it does, in the server's words.** The phrase is
 * `merge_mode_phrases`, sent with the state, so no copy of it lives here (task-309,
 * task-602).
 *
 * **`automerge` is offered disabled when this project has no scripted finish**, rather
 * than silently dropped. An automerge runs through `agentjobs finish
 * --automerge-release`, and a machine without the finish has no sanctioned mechanism for
 * one; picking it there would produce a run told it may merge with no way to.
 * Disabled-with-a-reason is right where omitting it is wrong, because the fix is one line
 * of the reader's own config and they can only make it if they know it is the cause.
 */
function MergeModeChoice({
  id,
  state,
  value,
  busy,
  onChange,
  className = "rounded-lg border border-dark-border bg-dark-bg p-2 text-sm text-dark-text focus:border-sky-500 focus:outline-none",
}: {
  id: string;
  state: DispatchStateView | null;
  value: string;
  busy: boolean;
  onChange: (next: string) => void;
  className?: string;
}) {
  const modes = state?.offerable_merge_modes ?? [];
  // One option means there is nothing to choose and a pulldown saying so is furniture --
  // the same rule the group select uses. A project that does not allow automerge offers
  // only `review`, so it shows no chooser at all.
  if (modes.length <= 1) return null;
  return (
    <div className="flex items-center gap-2">
      <label htmlFor={id} className="text-sm text-dark-muted">
        Merge mode
      </label>
      <select
        id={id}
        value={value}
        disabled={busy}
        onChange={(event) => onChange(event.target.value)}
        className={className}
      >
        <option value="">
          {state?.merge_mode ? `Project default (${state.merge_mode})` : "Project default"}
        </option>
        {modes.map((name) => {
          const blocked = automergeNeedsFinish(name, state);
          const says = blocked ? FINISH_REQUIRED : mergeConsequence(name, state);
          return (
            <option key={name} value={name} disabled={blocked}>
              {says ? `${capitalised(name)} — ${says}` : capitalised(name)}
            </option>
          );
        })}
      </select>
    </div>
  );
}

/** Why `automerge` cannot be picked on a project that has not switched the finish on. */
const FINISH_REQUIRED = "needs finish.enabled on this project";

/**
 * Whether choosing this mode would grant a merge the project cannot actually perform.
 *
 * Only `automerge` runs through the scripted finish; `review` is unaffected by the switch.
 */
function automergeNeedsFinish(mode: string, state: DispatchStateView | null): boolean {
  return mode === "automerge" && !state?.finish_enabled;
}

/**
 * What this mode does, as the server words it, lower-cased to sit inside a sentence.
 *
 * Null when the server sent no phrase for it, and the callers render nothing at all in
 * that case. Saying something vague would be worse than saying nothing: this sentence is
 * the one a reader decides on.
 */
function mergeConsequence(mode: string, state: DispatchStateView | null): string | null {
  const phrase = state?.merge_mode_phrases?.[mode];
  return phrase ? phrase.charAt(0).toLowerCase() + phrase.slice(1) : null;
}

function capitalised(word: string): string {
  return word.charAt(0).toUpperCase() + word.slice(1);
}

/** What the run will be, and whose name goes on it. Both worth reading before clicking. */
function DispatchRunnerNote({
  state,
  user,
  runner: chosenRunner,
  group,
  mergeMode: chosen,
}: {
  state: DispatchStateView | null;
  user: string;
  runner: string;
  group: string;
  mergeMode: string;
}) {
  // The chosen mode outranks the project's, so the sentence has to name what will
  // actually run rather than what the config says -- the same reason the group branch
  // below names the overriding group. Saying "review" beside a pulldown reading
  // `automerge` is the one sentence here that would be reliably wrong.
  const effective = chosen || state?.merge_mode;
  const consequence = effective ? mergeConsequence(effective, state) : null;
  const mergeMode = (
    <>
      merge mode <strong className="text-dark-text">{effective}</strong>
      {consequence ? <>: {consequence}</> : null}
      {/* Push is per project and never the merge mode's (task-021). Named only when it
          is on, because `false` is the answer everywhere today and a sentence repeating
          the universal default on every task is noise. */}
      {state?.push ? <>, and pushes</> : null}, authorised by{" "}
      <strong className="text-dark-text">{user}</strong>
    </>
  );
  if (chosenRunner) {
    return (
      <span className="text-sm text-dark-muted">
        Agent <strong className="text-dark-text">{runnerLabel(state, chosenRunner)}</strong>,{" "}
        {mergeMode}
      </span>
    );
  }
  // A group picked for this one dispatch outranks everything the project says, and the
  // browser holds no member list to resolve it with -- so it names the group that will
  // choose rather than guessing which member wins. Leaving the project's runner on
  // screen beside an overriding group would be the one sentence here that is reliably
  // wrong.
  if (group) {
    return (
      <span className="text-sm text-dark-muted">
        Automatic group <strong className="text-dark-text">{humanizeChoiceName(group)}</strong>,{" "}
        {mergeMode}
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
      Agent <strong className="text-dark-text">{runnerLabel(state, runner)}</strong>
      {state?.resolved_group ? (
        <>
          {" "}
          from <strong className="text-dark-text">{humanizeChoiceName(state.resolved_group)}</strong>
        </>
      ) : null}
      , {mergeMode}
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
            <div className="flex flex-col gap-2 @min-[768px]:flex-row @min-[768px]:items-center @min-[768px]:justify-between">
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
  /** Arm the pull mode with the bound and merge mode the person chose (task-462). */
  onArm?: (choice: PullArmChoice) => Promise<void> | void;
  /** Stop it starting anything more. Kills nothing. */
  onDisarm?: () => Promise<void> | void;
};

/**
 * What the Arm control submits: a bound, and optionally a merge mode.
 *
 * The bound is not optional and has no default here, which mirrors the API and is the
 * point of the control rather than a validation detail: the difference between "three
 * runs" and "all night" is the whole decision, and a form that pre-filled it would make
 * that decision for the person in the one place they are entitled to be asked.
 */
export type PullArmChoice = {
  bound_kind: "starts" | "until" | "open";
  starts?: number | null;
  until?: string | null;
  merge_mode?: string | null;
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
 * The pull mode's control: arm this project's backlog, or stop it (task-462).
 *
 * **The bound is a radio group, and it opens on the narrowest one.** Three starts and
 * "until disarmed" are different orders of magnitude of spend, so the default is not a
 * neutral choice -- but it does not have to be neutral, it has to be safe. A form that
 * opened on "until I disarm it" would spend the most on a person who pressed Arm without
 * reading; one that opens on three starts spends the least on the same person, and
 * anybody who wants the open-ended bound has to say so by clicking it. What is *not*
 * offered is arming with no bound at all, which is what the API refuses.
 *
 * **The merge mode says what it does, not what it is called.** The same chooser and
 * the same server-sent `merge_mode_phrases` the dispatch panel uses, for a reason that is
 * stronger here than there: a mode chosen at arming time decides what happens to *every*
 * branch the pull mode produces while nobody is watching.
 *
 * Armed, the control collapses to what a person needs while it is running: who armed it,
 * what is left of the bound, what it will start next -- so a reorder is still possible
 * before it happens -- and Disarm. Disarm is rendered unconditionally and asks nothing,
 * on the rule the dispatch kill switch follows.
 */
export function PullModeControl({
  state,
  busy = false,
  onArm,
  onDisarm,
}: {
  state: DispatchStateView;
  busy?: boolean;
  onArm?: (choice: PullArmChoice) => Promise<void> | void;
  onDisarm?: () => Promise<void> | void;
}) {
  const [bound, setBound] = useState<PullArmChoice["bound_kind"]>("starts");
  const [starts, setStarts] = useState("3");
  const [until, setUntil] = useState("");
  const [mergeMode, setMergeMode] = useState("");
  const pull = state.pull ?? null;
  if (!onArm && !onDisarm) return null;

  if (pull?.armed) {
    return (
      <section
        data-testid="pull-mode"
        data-pull-armed="yes"
        aria-label="Pull mode"
        className="mt-6 rounded-lg border border-orange-800/70 bg-orange-950/20 p-4"
      >
        <h3 className="text-sm font-semibold text-orange-200">Pull mode is armed</h3>
        <p className="mt-1 text-sm text-dark-text" data-testid="pull-mode-bound">
          Armed by {pull.armed_by || "somebody"} · {pull.bound}
          {pull.merge_mode_phrase ? ` · ${pull.merge_mode_phrase}` : ""}
        </p>
        <p className="mt-1 text-xs text-dark-muted">
          Every free slot on this machine is filled with whatever the queue says is next.
          Each start goes through every dispatch gate, and a manual dispatch waiting for a
          slot starts before it does.
        </p>
        {pull.next_task_id && (
          <p className="mt-2 text-sm" data-testid="pull-mode-next">
            <span className="text-dark-muted">Next: </span>
            <span className="font-mono text-xs text-blue-400">{pull.next_task_id}</span>
            <span className="ml-2 text-dark-text">{pull.next_task_title}</span>
          </p>
        )}
        <button
          type="button"
          data-testid="pull-mode-disarm"
          disabled={busy}
          onClick={() => void onDisarm?.()}
          className="touch-target mt-4 rounded-lg bg-red-700 px-4 font-semibold text-white hover:bg-red-600 disabled:opacity-60"
        >
          Disarm
        </button>
        <p className="mt-2 text-xs text-dark-muted">
          Disarming stops takeoffs. Runs already going keep running — each was authorised
          on its own and has its own merge gate.
        </p>
      </section>
    );
  }

  const chosen = mergeMode || state.merge_mode || "";
  const consequence = mergeConsequence(chosen, state);
  return (
    <section
      data-testid="pull-mode"
      data-pull-armed="no"
      aria-label="Pull mode"
      className="mt-6 rounded-lg border border-dark-border bg-dark-bg p-4"
    >
      <h3 className="text-sm font-semibold">Pull mode</h3>
      <p className="mt-1 text-sm text-dark-muted">
        Keep filling free run slots with whatever this project&rsquo;s queue says is next,
        with no further click, until the bound below runs out or you disarm it.
      </p>
      {pull?.last_state && (
        <p className="mt-2 text-xs text-dark-muted" data-testid="pull-mode-last">
          Last time: {pull.last_state}
          {pull.last_detail ? ` — ${pull.last_detail}` : ""}
        </p>
      )}

      <fieldset className="mt-4" disabled={busy}>
        <legend className="text-sm font-semibold">Stop after</legend>
        <div className="mt-2 space-y-2">
          <label className="flex items-center gap-2 text-sm">
            <input
              type="radio"
              name="pull-bound"
              value="starts"
              checked={bound === "starts"}
              onChange={() => setBound("starts")}
            />
            <span>this many starts</span>
            <input
              type="number"
              min={1}
              aria-label="Number of starts"
              value={starts}
              onChange={(event) => setStarts(event.target.value)}
              className="w-20 rounded-lg border border-dark-border bg-dark-surface p-1 text-dark-text"
            />
          </label>
          <label className="flex items-center gap-2 text-sm">
            <input
              type="radio"
              name="pull-bound"
              value="until"
              checked={bound === "until"}
              onChange={() => setBound("until")}
            />
            <span>this moment</span>
            <input
              type="datetime-local"
              aria-label="Stop at"
              value={until}
              onChange={(event) => setUntil(event.target.value)}
              className="rounded-lg border border-dark-border bg-dark-surface p-1 text-dark-text"
            />
          </label>
          <label className="flex items-center gap-2 text-sm">
            <input
              type="radio"
              name="pull-bound"
              value="open"
              checked={bound === "open"}
              onChange={() => setBound("open")}
            />
            <span>nothing — keep going until I disarm it</span>
          </label>
        </div>
      </fieldset>

      <div className="mt-4 flex flex-wrap items-center gap-2">
        <MergeModeChoice
          id="pull-merge-mode"
          state={state}
          value={mergeMode}
          busy={busy}
          onChange={setMergeMode}
          className="rounded-lg border border-dark-border bg-dark-surface p-2 text-sm text-dark-text"
        />
      </div>

      {/* What the chosen mode will do to every branch the pull mode produces. Stated
          outside the pulldown as well as inside it, because this is the sentence the
          decision turns on and an <option> is read once and then not looked at again. */}
      {consequence && (
        <p className="mt-2 text-sm" data-testid="pull-mode-consequence">
          <span className="text-dark-muted">Each pulled run </span>
          <strong className={chosen === "automerge" ? "text-orange-300" : "text-dark-text"}>
            {consequence}
          </strong>
          {chosen === "automerge" && !state.push && (
            <span className="text-dark-muted">
              {" "}
              — nothing is pushed, so a merge you did not want is revertible here.
            </span>
          )}
        </p>
      )}

      <button
        type="button"
        data-testid="pull-mode-arm"
        disabled={busy || !state.project_enabled || (bound === "until" && !until)}
        onClick={() =>
          void onArm?.({
            bound_kind: bound,
            starts: bound === "starts" ? Number(starts) || 0 : null,
            // A `datetime-local` value carries no zone; the person means their own
            // clock, so it is sent as a local moment and the server stores what it is
            // given. Appending a `Z` here would silently move the deadline by the
            // machine's offset.
            until: bound === "until" ? until : null,
            merge_mode: mergeMode || null,
          })
        }
        className="touch-target mt-4 rounded-lg bg-orange-700 px-4 font-semibold text-white hover:bg-orange-600 disabled:opacity-60"
      >
        Arm pull mode
      </button>
      {!state.project_enabled && (
        <p className="mt-2 text-xs text-dark-muted">
          Dispatch is off for this project, so there is nothing for the pull mode to start.
          Enable it above first.
        </p>
      )}
    </section>
  );
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
  onArm,
  onDisarm,
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
      ? runners.map((name) => ({
          value: name,
          label: runnerLabel(state, name),
          target: { runner: name },
        }))
      : [
          ...groups.map((name) => ({
            value: groupOptionValue(name),
            label: `group: ${humanizeChoiceName(name)}`,
            target: { group: name },
          })),
          ...runners.map((name) => ({
            value: name,
            label: `agent: ${runnerLabel(state, name)}`,
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

  // `@container` declares its own container query context. The `@min-[768px]:` rules in
  // this file were window queries until task-239 moved them onto the box -- which the
  // panel inside a task record needs and this page does not care about -- but a
  // container query with no container anywhere above it matches nothing at all, so
  // without this the settings page would render permanently narrow. Declaring one here
  // costs nothing and makes the rule mean the same thing on both surfaces.
  return (
    <section className="@container space-y-6" aria-label="Dispatch settings">
      <div className="rounded-xl border border-dark-border bg-dark-surface p-4 @min-[768px]:p-6">
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

        {/* Below the project switch and inside the same card, because arming is a
            statement about *this project's* dispatch and reads as nonsense above the
            control that decides whether it may dispatch at all. */}
        <PullModeControl state={state} busy={busy} onArm={onArm} onDisarm={onDisarm} />
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
