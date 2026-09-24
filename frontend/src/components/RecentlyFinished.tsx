import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";

import { listRecentClosuresApiRecentClosuresGetOptions } from "../api/generated/@tanstack/react-query.gen";
import type { ClosureView, RecentClosuresView } from "../api/types";
import { StatusChip } from "./StatusChip";

/**
 * What landed while you were not looking (task-460).
 *
 * The Dashboard could already say what is running and what is waiting on a person.
 * Neither answers the question the page is actually opened with once agents run most of
 * the time: work closes and merges overnight, and the only trace of it is a task that
 * has quietly left every list on the screen.
 *
 * **Machine-wide, like the slot board above it, and for the same reason.** The thing
 * that finished overnight is routinely in a different project from the one whose
 * Dashboard is open, so the server does the fan-out and every row carries its own
 * project name and its own `task_url`. A row for another project links into that
 * project, never into the one being looked at.
 *
 * **Finished means the task closed.** A run that ended without closing its task is not
 * finished work -- it is a run that stopped, which the slot board already says in words
 * a person can act on. Nothing on this list is inferred from a run.
 */

/**
 * How often the region is re-read.
 *
 * Slower than the runs list, which polls every two seconds while something is running.
 * A closure is a discrete event that happens a handful of times a day at most, and this
 * region is glanced at rather than watched: a reader who sees a row half a minute late
 * has lost nothing, and the page already pays for two faster pollers.
 *
 * Not `false`, for the reason `LiveRuns` gives: nothing on this page causes the next
 * closure. It will come from an epic walk, a scripted finish or another project's
 * session, none of which can invalidate this query.
 */
export const CLOSURES_POLL_MS = 30_000;

/** The machine-wide closures query, so a second reader of it costs no request. */
export function useRecentClosures(): RecentClosuresView | null {
  const query = useQuery({
    ...listRecentClosuresApiRecentClosuresGetOptions(),
    refetchInterval: CLOSURES_POLL_MS,
  });
  return query.data ?? null;
}

/**
 * How long ago something closed, in the words a reader of this region wants.
 *
 * Deliberately not `formatElapsed`, which prints a *duration* -- `3h 07m` -- because it
 * describes a run that is still going. These rows describe a moment that has passed, and
 * `3h ago` is what somebody asking "what happened overnight" reads. One unit only: the
 * minutes inside "two days ago" are noise.
 *
 * The seconds come from the server. The phone reading this page is not on the clock that
 * wrote the stamp, and a browser subtracting its own `Date.now()` from a UTC string
 * would print an hour's error on a machine with a wrong clock and no way to see it.
 */
export function closureAge(seconds: number): string {
  const whole = Math.max(0, Math.floor(seconds));
  if (whole < 60) return "just now";
  const minutes = Math.floor(whole / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

/**
 * One closure, on one line where there is room for one.
 *
 * `completed` is the common case and the other three are the ones worth noticing, so the
 * outcome is said rather than flattened into "done". A region that said *done* for a task
 * somebody cancelled would be hiding the one difference these rows are scanned for.
 *
 * The chip is the server's `display_status` and `status_category` for the closure, from
 * the function a task read uses (task-562), so this row says exactly what the task list
 * says about the same task: "Completed" in grey, and the other three in grey struck
 * through. It used to print the raw lowercase outcome, and Completed in green.
 *
 * The id, outcome, project and age wrap under the title below 560px rather than being
 * dropped, because the project is exactly what a phone reader needs here: the whole point
 * of the region is that half its rows belong to some other project.
 */
function ClosureRow({ closure }: { closure: ClosureView }) {
  return (
    <Link
      to={closure.task_url}
      data-testid="closure-row"
      data-task={closure.task_id}
      className="touch-target block px-4 py-2 transition hover:bg-dark-border/40"
    >
      <div className="flex flex-col gap-0.5 min-[560px]:flex-row min-[560px]:items-baseline min-[560px]:gap-3">
        <div className="flex min-w-0 flex-1 items-baseline gap-2">
          <span className="shrink-0 font-mono text-xs text-blue-400">{closure.task_id}</span>
          <span className="truncate text-sm text-dark-text">{closure.task_title}</span>
        </div>
        <div className="flex shrink-0 items-center gap-2 text-xs text-dark-muted">
          <span data-outcome={closure.outcome} className="inline-flex">
            <StatusChip category={closure.status_category} label={closure.display_status} />
          </span>
          <span className="truncate">{closure.project_name}</span>
          <time dateTime={closure.closed_at} className="whitespace-nowrap">
            {closureAge(closure.age_seconds)}
          </time>
        </div>
      </div>
    </Link>
  );
}

/**
 * The region itself, sized and worded by the answer rather than by this file.
 *
 * `window_days` comes off the wire so the empty sentence stays true if the endpoint's
 * window ever moves; a page that hard-coded seven would go on saying seven.
 *
 * `body` is null until the machine-wide answer arrives. The region renders its heading
 * and nothing else in that moment rather than an empty state, because *nothing finished
 * this week* and *we have not been told yet* are different sentences and only one of
 * them is worth reading.
 */
export function RecentlyFinished({
  body,
  projectId,
}: {
  body: RecentClosuresView | null;
  projectId: string;
}) {
  const closures = body?.closures ?? [];
  return (
    <section
      data-testid="recently-finished"
      className="shrink-0 rounded-lg border border-dark-border bg-dark-surface"
    >
      <div className="flex items-baseline justify-between gap-4 border-b border-dark-border px-4 py-2">
        <h2 className="text-sm font-medium text-dark-text">Recently finished</h2>
        <Link
          to={`/p/${encodeURIComponent(projectId)}/tasks?status=closed`}
          className="touch-target whitespace-nowrap text-xs text-blue-400 hover:text-blue-300"
        >
          All closed →
        </Link>
      </div>
      <div className="divide-y divide-dark-border">
        {closures.map((closure) => (
          <ClosureRow key={`${closure.project_id}/${closure.task_id}`} closure={closure} />
        ))}
        {body !== null && closures.length === 0 && (
          <p className="px-4 py-3 text-sm text-dark-muted">
            Nothing has finished in the last {body.window_days} days.
          </p>
        )}
      </div>
    </section>
  );
}
