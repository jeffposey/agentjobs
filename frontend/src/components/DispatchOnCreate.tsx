import { Link } from "react-router-dom";

import type { DispatchStarted, Task } from "../api/generated";
import type { DispatchStateView } from "../api/types";
import { readRefusal } from "../api/mutation-error";
import { REFUSAL_ACTIONS, type DispatchRefusal } from "./DispatchPanel";

/**
 * Starting an agent on the task you are filing, from the form that files it (task-176).
 *
 * The gap this closes is between filing and starting: for a small obvious fix, the
 * decision to work on it was already made at the moment of noticing, and making
 * somebody open the task afterwards to press a second button is asking them to decide
 * twice. So both creating surfaces -- the create form and the issue reporter -- carry
 * one checkbox, and checking it files the task and starts a run in the same gesture.
 *
 * **It is unchecked by default and is not remembered.** Starting an agent spends money
 * on this machine unattended, and the moment of filing is the moment you know least
 * about whether the task is ready to be worked. Someone who wants it wants it
 * deliberately, per task; a default-on box, or a sticky one, would turn "spend money"
 * into a setting somebody forgot they changed.
 *
 * **No dispatch guard is relaxed, exempted or widened for this**, and nothing here is a
 * second implementation of one. A human filling in a form and pressing create is a human
 * act, so a dispatch may follow it -- the existing human-clocked rule is satisfied by
 * the creation entry the create itself writes, which is why this surface needs no
 * exemption and gets none. What happens on screen is exactly two requests in order:
 * `POST /tasks`, then the ordinary `POST /tasks/{id}/dispatch` the Dispatch button
 * posts, with the same body and every gate in front of it.
 *
 * **Two requests means two outcomes, and they are reported separately.** A create that
 * succeeded and a dispatch that was refused is a task that exists and is not running,
 * and the person is told exactly that. Nothing is ever rolled back: a refused dispatch
 * is not a reason to delete a record somebody just wrote.
 */

/**
 * Whether the checkbox may be checked, and what to say when it may not.
 *
 * A discriminated union rather than a boolean and an optional message, so a caller
 * cannot render a reason for an open gate or an empty disabled control.
 */
export type StartOnFileGate =
  | { allowed: true }
  | { allowed: false; reason: string; message: string };

/**
 * Could a run be started from here right now, and if not, which gate says so.
 *
 * Read off the same `GET /dispatch` answer every other dispatch surface reads, so this
 * checkbox and the task page's Dispatch button cannot disagree about the state of the
 * machine. The order is the order the reasons matter in: the machine, then the person,
 * then the task.
 *
 * `null` state is a gate too, and deliberately so. The control is disabled while the
 * answer is still in flight rather than enabled and hopeful: an enabled box that turns
 * out to have been unavailable is the "enabled-then-failing" this is built to avoid,
 * and it would spend somebody's create on a refusal.
 */
export function startOnFileGate({
  state,
  user,
  identityDetail,
  lifecycle,
}: {
  /** Machine and project gates. Null while `GET /dispatch` is still in flight. */
  state: DispatchStateView | null;
  /** Who the run would be attributed to, or null when nothing resolves to a person. */
  user: string | null;
  /** Why no person resolved, said in the identity's own words. */
  identityDetail: string;
  /** The starting state the form will file this task in. */
  lifecycle: "draft" | "ready";
}): StartOnFileGate {
  if (!state) {
    return {
      allowed: false,
      reason: "state_unknown",
      message: "Still reading whether this machine can start an agent.",
    };
  }
  if (!state.can_dispatch) {
    // The server's own sentence, under the server's own code. Restating a gate in the
    // browser's words is how two surfaces come to describe the same closed gate
    // differently; `REFUSAL_ACTIONS` adds the remedy, exactly as everywhere else.
    const refusal = state.refusal;
    return {
      allowed: false,
      reason: refusal?.reason ?? "dispatch_error",
      message: refusal?.message ?? "This machine cannot dispatch right now.",
    };
  }
  if (!user) {
    // The same rule the task page states: the run's authorising entry has to name a
    // real person, and the page already knows it has none. Disabled rather than
    // pressable into `authorizer_not_human`.
    return { allowed: false, reason: "no_signed_in_user", message: identityDetail };
  }
  if (lifecycle !== "ready") {
    // A UI rule, and the one place this surface is *stricter* than the dispatch API --
    // which is not a guard being relaxed but the opposite. A task filed as a draft says
    // in its own lifecycle that a person still has to decide it is worth doing, and
    // starting an agent on it in the same gesture contradicts that. It is also what
    // makes task-175's sequence the natural one: draft it properly, mark it ready, then
    // start it. The alternative -- letting a draft be dispatched, as the API would --
    // was rejected because a one-line stub filed on a phone is exactly the record an
    // unattended run has nothing to work from.
    return {
      allowed: false,
      reason: "not_ready_for_an_agent",
      message:
        "This task is being filed as a draft, which says a person still has to decide " +
        "it is worth doing. Mark it ready for an agent to start one on it.",
    };
  }
  return { allowed: true };
}

/**
 * The checkbox itself, with the reason underneath when it is closed.
 *
 * `data-refusal-reason` carries the gate's code for the same reason every other
 * dispatch surface carries it: a test, and a person reading the DOM, should be able to
 * tell *which* gate is shut without matching on a sentence.
 */
export function StartOnFileCheckbox({
  gate,
  checked,
  onChange,
  machineFull = false,
}: {
  gate: StartOnFileGate;
  checked: boolean;
  onChange: (next: boolean) => void;
  /**
   * Every slot on this machine is taken (task-461's `machine_full`).
   *
   * A warning rather than a closed gate, because that is the line the server itself
   * draws: `can_dispatch` stays true and a full machine is a question to ask the person
   * pressing, not a reason to withhold the control. Said here so the answer -- filed,
   * not started -- is not a surprise afterwards.
   */
  machineFull?: boolean;
}) {
  const blocked = !gate.allowed;
  return (
    <div
      className="rounded-lg border border-dark-border bg-dark-bg p-4"
      data-refusal-reason={blocked ? gate.reason : undefined}
    >
      <label className={`flex items-start gap-3 font-medium ${blocked ? "opacity-60" : ""}`}>
        <input
          type="checkbox"
          className="mt-1"
          checked={checked}
          disabled={blocked}
          onChange={(event) => onChange(event.target.checked)}
        />
        <span>
          Start an agent on it now
          <span className="block text-xs font-normal text-dark-muted">
            Off by default: this spends money on this machine, unattended, on a
            specification you wrote a moment ago.
          </span>
        </span>
      </label>
      {blocked && (
        <p role="status" className="mt-2 text-xs text-orange-200">
          {gate.message}
          {REFUSAL_ACTIONS[gate.reason] ? ` ${REFUSAL_ACTIONS[gate.reason]}` : ""}
        </p>
      )}
      {!blocked && machineFull && (
        <p role="status" className="mt-2 text-xs text-amber-200">
          Every run slot on this machine is taken right now, so this may be filed without
          starting. You will be told which happened.
        </p>
      )}
    </div>
  );
}

/**
 * What a create asked to start an agent actually did.
 *
 * Two independent outcomes in one value, because the two requests succeed and fail
 * independently and a single "it worked" would report success for both when only one
 * happened. `not_asked` is the ordinary case -- the box was unchecked -- and is a
 * member of this union rather than an absent field so a renderer has to handle it.
 */
export type StartOutcome =
  | { kind: "not_asked" }
  | { kind: "started"; runId: string }
  | { kind: "queued"; place: number }
  | { kind: "refused"; refusal: DispatchRefusal };

export type FiledOutcome = {
  /** The task that now exists. It exists whatever happened next. */
  task: Task;
  start: StartOutcome;
};

/**
 * File the task, then start an agent on it if that was asked for.
 *
 * Shared by both creating surfaces because the *sequencing* is the part with a rule in
 * it, and two copies of it would eventually disagree about the rule. Specifically:
 *
 * - A create that fails throws, and this never swallows it. Nothing was filed, so there
 *   is one outcome and the caller's own error path is right.
 * - A dispatch that is refused **never** propagates and **never** undoes the create.
 *   The task exists; rolling it back because a run could not start would destroy a
 *   record a person deliberately wrote, to tidy up after a machine being busy.
 * - A dispatch accepted into the slot queue is not a dispatch that started (task-459),
 *   and says so. Reporting a queued dispatch as running would tell somebody an agent is
 *   working when nothing has.
 */
export async function fileAndMaybeStart({
  create,
  start,
  wanted,
}: {
  create: () => Promise<Task>;
  start: (taskId: string) => Promise<DispatchStarted>;
  wanted: boolean;
}): Promise<FiledOutcome> {
  const task = await create();
  if (!wanted) return { task, start: { kind: "not_asked" } };
  try {
    const answer = await start(task.id);
    if (answer?.queued) {
      return { task, start: { kind: "queued", place: answer.queue_position || 1 } };
    }
    return { task, start: { kind: "started", runId: answer.run_id } };
  } catch (caught) {
    const read = readRefusal(caught);
    return {
      task,
      start: {
        kind: "refused",
        refusal: read
          ? { reason: read.code, message: read.message, suggestedAction: read.suggestedAction }
          : {
              reason: "unreachable",
              message: "AgentJobs could not be reached to start a run.",
              suggestedAction: "Check that the server is still running, then dispatch the task.",
            },
      },
    };
  }
}

/**
 * The confirmation, naming both outcomes and linking to the task.
 *
 * One component for both surfaces, so "filed, not started" reads identically wherever
 * you filed from. The link is always to the task, because that is where the run is
 * visible and where the Dispatch button is for a start that did not happen.
 *
 * **The card carries its own heading, inside it, at the top left** -- the same place
 * "Core specification" and "Planning and relationships" put theirs on the form this
 * replaces. The heading is what the card *is* ("Filed as task-003"); the task's own
 * title goes underneath it as content, because a title is data somebody typed and
 * belongs in the body of a card rather than in a heading slot.
 */
export function FiledNotice({
  taskId,
  taskTitle,
  taskHref,
  start,
  onNavigate,
}: {
  taskId: string;
  /** What the person called it. Content, not a heading -- see the note above. */
  taskTitle?: string;
  taskHref: string;
  start: StartOutcome;
  /** Called when the link is followed, so a dialog can close itself. */
  onNavigate?: () => void;
}) {
  const refused = start.kind === "refused";
  return (
    <div className="space-y-3">
      <div
        role="status"
        data-task-id={taskId}
        data-start-outcome={start.kind}
        className={`rounded-lg border p-4 ${
          refused
            ? "border-orange-600/60 bg-orange-950/40 text-orange-100"
            : "border-green-600/60 bg-green-950/40 text-green-200"
        }`}
      >
        <h3 className="text-lg font-semibold">
          Filed as <strong>{taskId}</strong>
        </h3>
        {taskTitle && <p className="mt-1 text-sm opacity-80">{taskTitle}</p>}
        <p className="mt-2">
          {start.kind === "not_asked" && <>No agent was started on it.</>}
          {start.kind === "started" && <>An agent is working on it — run {start.runId}.</>}
          {start.kind === "queued" && (
            <>
              Nothing has started yet: every run slot is taken, so this dispatch is waiting
              for the next free one, place {start.place} in line.
            </>
          )}
          {start.kind === "refused" && (
            <>
              <strong>It was not started.</strong> {start.refusal.message}
            </>
          )}
        </p>
      </div>
      {refused && start.kind === "refused" && (
        <p
          data-refusal-reason={start.refusal.reason}
          className="text-xs text-orange-200"
        >
          {start.refusal.suggestedAction || REFUSAL_ACTIONS[start.refusal.reason] || ""}
        </p>
      )}
      <p className="text-sm">
        <Link
          to={taskHref}
          onClick={onNavigate}
          className="font-semibold text-blue-300 underline hover:text-blue-200"
        >
          Open {taskId}
        </Link>
        <span className="text-dark-muted">
          {" "}
          — the record, and whatever run it has.
        </span>
      </p>
    </div>
  );
}
