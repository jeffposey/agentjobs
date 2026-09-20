import type { StalledTaskRead } from "../../api/types";

/**
 * Rendering for a task in the waiting set that nobody is working (task-499).
 *
 * The server decides *whether* a task is stalled and *why*; this file decides only how
 * to say it. That split is why `StalledTaskRead` carries a reason and two numbers
 * rather than a sentence -- a client matching on the words of a label is what
 * ENGINEERING.md's rendered-value rule exists to prevent, and there are two callers
 * here already.
 *
 * The distinction this has to draw on screen is not subtle and is easy to lose: a
 * stalled task sits in a panel headed "Blocked on You" beside tasks at the merge gate,
 * and its own badge reads *In progress (claude)* because that is genuinely what the
 * record says. Without a label saying otherwise, the row reads as work in flight that
 * someone has filed in the wrong place -- which is exactly the reading that cost
 * twenty-two hours on 2026-09-19.
 */

/** `3h 12m`; minutes below an hour, never seconds. This is an hours-scale signal. */
export function quietPhrase(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds / 60));
  const hours = Math.floor(total / 60);
  const minutes = total % 60;
  return hours ? `${hours}h ${minutes}m` : `${minutes}m`;
}

/** The badge on a stalled row: what is wrong, and for how long. */
export function stallBadge(stall: StalledTaskRead): string {
  const quiet = quietPhrase(stall.quiet_seconds);
  return stall.reason === "undelivered_handback"
    ? `Feedback undelivered ${quiet}`
    : `No agent for ${quiet}`;
}

/**
 * The line that replaces the ball prompt on a stalled row.
 *
 * The prompt itself would be actively misleading here: it is addressed to an agent
 * ("Address the review feedback") and the whole finding is that no agent is going to
 * read it. What the person needs is what only they can decide -- re-dispatch it, take
 * it over, or let it sit.
 */
export function stallExplanation(stall: StalledTaskRead): string {
  return stall.reason === "undelivered_handback"
    ? `Your feedback has been queued for ${stall.run_id} for ${quietPhrase(stall.quiet_seconds)} and that run has not moved. Nothing will deliver it on its own.`
    : `Claimed, but nothing has been running against it for ${quietPhrase(stall.quiet_seconds)}. Re-dispatch it, take it over, or leave it.`;
}

/** The stalls by task id, for a panel drawing rows it already holds. */
export function stallsByTask(
  stalled: Array<StalledTaskRead> | undefined,
): Map<string, StalledTaskRead> {
  return new Map((stalled ?? []).map((stall) => [stall.task_id, stall]));
}
