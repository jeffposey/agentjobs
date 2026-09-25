import { Link } from "react-router-dom";

import type { LiveRunsView } from "../api/types";
import { ATTENTION_BADGE_MAX } from "./AttentionBadge";
import { capacitySentence, runningCount } from "./LiveRuns";

/**
 * The header's one status readout, attached to the Dashboard tab (task-588).
 *
 * Until task-588 the bar carried two badges: a green running count on the Runs link
 * (task-328) and a red waiting-on-you count beside the project switcher (task-338). The
 * owner never used the Runs tab, so the green badge sat on a destination nobody went to,
 * and the two numbers that answer "what is this machine doing for me" were in two places.
 * They are one readout now, in three parts:
 *
 * - **slots used** -- `occupied/max_concurrent_runs`, the server's own words and the
 *   concurrency guard's. Over the ceiling it reads `3/2` rather than clamping, for the
 *   reason `capacitySentence` gives. Left out on a machine with no dispatch configured:
 *   there is no denominator, and the running count is already the green part.
 * - **red dot** -- tasks stopped waiting on you, `attention.blocking` exactly as the old
 *   badge counted it, capped at {@link ATTENTION_BADGE_MAX}+ the way it was.
 * - **green dot** -- being worked right now: `runningCount`, runs plus finishes.
 *
 * **Every part is always drawn, and a zero recedes rather than vanishing.** The old red
 * badge disappeared at zero on the argument that an alarm always on screen stops being
 * read. That held for a badge alone; beside two readouts, a part that comes and goes
 * makes the pill change width and leaves "nothing waiting" indistinguishable from "not
 * loaded". So a zero is a grey dot and muted digits, and a non-zero is the colour.
 *
 * **It is a link to the Dashboard, and following it acknowledges the attention episode**
 * -- task-422's act, carried over from the red badge rather than extended to the plain
 * Dashboard link, because opening the Dashboard out of habit is not the same as having
 * looked at what stopped.
 *
 * Prop-driven like the badges it replaces, so {@link PrimaryNav}'s tests need no query
 * client; the shell reads both queries and hands the answers in.
 */
export function NavStatus({
  runs,
  waiting,
  projectId,
  onAcknowledge,
}: {
  /** The machine-wide live-runs view, or `null` before it has been read. */
  runs: LiveRunsView | null;
  /** `attention.blocking` for this project, or `null` before it has been read. */
  waiting: number | null;
  projectId: string;
  onAcknowledge?: () => void;
}) {
  const waitingCount = Math.max(0, waiting ?? 0);
  const workingCount = runningCount(runs);
  const slots = runs?.dispatch_configured ? `${runs.occupied}/${runs.max_concurrent_runs}` : null;

  const parts = [
    ...(runs ? [capacitySentence(runs)] : []),
    `${waitingCount} waiting on you`,
    `${workingCount} being worked`,
  ];
  const label = parts.join(" · ");

  return (
    <Link
      to={`/p/${encodeURIComponent(projectId)}`}
      data-testid="nav-status"
      aria-label={label}
      title={label}
      onClick={() => onAcknowledge?.()}
      className="flex h-7 shrink-0 items-center gap-1.5 rounded-full border border-dark-border px-2 text-xs font-semibold tabular-nums hover:bg-dark-border"
    >
      {slots !== null && (
        <span data-testid="nav-status-slots" className="text-dark-text">
          {slots}
        </span>
      )}
      <Count testId="nav-status-waiting" count={waitingCount} dot="bg-red-500" />
      <Count testId="nav-status-working" count={workingCount} dot="bg-green-500" />
    </Link>
  );
}

function Count({ testId, count, dot }: { testId: string; count: number; dot: string }) {
  const lit = count > 0;
  return (
    <span
      data-testid={testId}
      data-count={count}
      className={`inline-flex items-center gap-0.5 ${lit ? "text-white" : "text-dark-muted"}`}
    >
      <span aria-hidden="true" className={`h-2 w-2 rounded-full ${lit ? dot : "bg-slate-600"}`} />
      {count > ATTENTION_BADGE_MAX ? `${ATTENTION_BADGE_MAX}+` : count}
    </span>
  );
}
