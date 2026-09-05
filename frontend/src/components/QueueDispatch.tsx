import { Link } from "react-router-dom";

import type { DispatchStateView } from "../api/types";
import { REFUSAL_ACTIONS, type DispatchRefusal } from "./DispatchPanel";

/**
 * Starting the next task from the Dashboard (task-337).
 *
 * The Dashboard's job is to answer "what do I do next", and until now its answer to the
 * best case -- nothing needs you, work is ready -- was a link to a task page where the
 * button lives. This is that button, brought to where the question is asked.
 *
 * **It is deliberately the one-click form and nothing more.** The task page's
 * {@link DispatchPanel} carries a group pulldown, a posture chooser, a brief textarea,
 * the run list and every refusal a run can produce. None of that belongs on a panel
 * whose whole point is that it fits above the fold and offers three tasks: a reader who
 * wants to choose a runner or write a brief is choosing, and choosing happens on the
 * task's own page. So a task the record cannot brief gets a link there rather than a
 * box here, which is the same rule stated from the other end -- the special occasion is
 * still special, it just happens somewhere with room for it.
 */

export type QueueDispatchProps = {
  /** Machine and project gates, from the same endpoint every other dispatch surface reads. */
  state: DispatchStateView | null;
  /** Who the run would be attributed to, or null when nothing resolves to a person. */
  user: string | null;
  /** Why no person resolved, said in the identity's own words. */
  identityDetail: string;
  /**
   * Whether the task record could brief an agent that has never seen it.
   *
   * Computed by the caller from `spec.description`, the same field the server checks
   * and the same expression {@link TaskDetail} uses. False sends the reader to the task
   * page, where the brief box is.
   */
  canBrief: boolean;
  /** Where that task lives, for the cases this button does not press. */
  taskHref: string;
  /** A dispatch for this task is in flight. */
  busy?: boolean;
  /** The last refusal from pressing this button, if it was this task's. */
  refusal?: DispatchRefusal | null;
  onDispatch: () => void;
};

/** The gate that stops a run before a click, with the sentence explaining it. */
export function queueGateRefusal(state: DispatchStateView | null): DispatchRefusal | null {
  if (!state || state.can_dispatch || !state.refusal) return null;
  return { reason: state.refusal.reason, message: state.refusal.message };
}

/**
 * The one line the panel prints when this machine cannot dispatch at all.
 *
 * At the foot of the panel rather than beside every row, because a closed gate is a
 * fact about the machine and the project: repeating it three times would say the same
 * thing three times and make the panel about the obstacle rather than the work.
 *
 * Silent on a machine with no `dispatch.yaml`, exactly as the task panel is. There is
 * nothing to switch on and nothing to explain, and a permanent note about a feature the
 * owner never configured is clutter on the page they open most.
 */
export function QueueDispatchGate({
  state,
  projectId,
}: {
  state: DispatchStateView | null;
  projectId: string;
}) {
  if (!state?.configured) return null;
  const refusal = queueGateRefusal(state);
  if (!refusal) return null;
  const action = refusal.suggestedAction || REFUSAL_ACTIONS[refusal.reason];
  return (
    <p
      role="status"
      data-refusal-reason={refusal.reason}
      className="mt-3 text-xs text-orange-200"
    >
      {refusal.message}{action ? ` ${action}` : ""}{" "}
      <Link
        to={`/p/${encodeURIComponent(projectId)}/dispatch`}
        className="text-blue-400 underline hover:text-blue-300"
      >
        Dispatch settings
      </Link>
    </p>
  );
}

export function QueueDispatch({
  state,
  user,
  identityDetail,
  canBrief,
  taskHref,
  busy = false,
  refusal = null,
  onDispatch,
}: QueueDispatchProps) {
  // Nothing at all where dispatch is off or unconfigured. The panel's foot says why
  // once, and a row that offered a dead control beside every task would be worse than
  // the link-only panel this replaced.
  if (!state?.configured || !state.can_dispatch) return null;

  if (!user) {
    // Disabled rather than pressable-into-a-refusal, and for the reason the task panel
    // gives: the run needs somebody's name on it and the page already knows it has none.
    return (
      <span className="flex flex-col items-end gap-1">
        <button
          type="button"
          disabled
          data-refusal-reason="no_signed_in_user"
          className="touch-target rounded-lg bg-sky-600 px-3 text-sm font-semibold text-white opacity-60"
        >
          ▶ Dispatch
        </button>
        <span className="max-w-[16rem] text-right text-xs text-orange-200">{identityDetail}</span>
      </span>
    );
  }

  if (!canBrief) {
    // The record cannot brief an agent, so this dispatch needs a human's text and the
    // box for it is on the task page. Saying so is better than a button that opens a
    // textarea in a panel with no room for one.
    return (
      <Link
        to={taskHref}
        className="touch-target rounded-lg border border-sky-700/60 px-3 text-sm font-semibold text-sky-300 hover:bg-sky-950/40"
      >
        Brief and dispatch →
      </Link>
    );
  }

  return (
    <span className="flex flex-col items-end gap-1">
      <button
        type="button"
        disabled={busy}
        onClick={onDispatch}
        className="touch-target rounded-lg bg-sky-600 px-3 text-sm font-semibold text-white hover:bg-sky-500 disabled:opacity-60"
      >
        {busy ? "Starting…" : "▶ Dispatch"}
      </button>
      {refusal && (
        <span
          role="alert"
          data-refusal-reason={refusal.reason}
          className="max-w-[16rem] text-right text-xs text-orange-200"
        >
          {refusal.message}
        </span>
      )}
    </span>
  );
}
