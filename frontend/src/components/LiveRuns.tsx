import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";

import { listLiveRunsApiRunsLiveGetOptions } from "../api/generated/@tanstack/react-query.gen";
import type { LiveRunView, LiveRunsView, MachineHolderView } from "../api/types";
import { formatElapsed } from "./DispatchPanel";
import { ResponsiveCell, ResponsiveTable, ResponsiveTableRow } from "./ResponsiveTable";

/**
 * What is running on this machine, across every project (task-328).
 *
 * Two surfaces, one query, on purpose. The Runs tab and the Dashboard's capacity row
 * render the same answer at different lengths, and react-query dedupes them by key --
 * so a Dashboard with the nav badge above it costs one request, not three.
 *
 * **Machine-wide, and the URL is not.** The page lives under `/p/:projectId/runs`
 * because the app shell, the header and the project switcher are all project-scoped and
 * a surface outside them would have no navigation at all. The *content* is not: every
 * row carries its own `project_id` and its own server-built `task_url`, so a row for
 * another project links into that project rather than into the one you are looking at.
 */

/** How often the runs list is re-read while something is running. */
export const BUSY_POLL_MS = 2_000;

/**
 * How often it is re-read while nothing is.
 *
 * Not `false`, which is what the per-task panel uses. That panel can stop, because the
 * only thing that starts a run on it is the button beside it, and that invalidates the
 * query itself. This list has no such guarantee: the next run to appear here will be
 * started from another project's page, from the CLI, or by an epic walk, and a badge
 * that had stopped polling would sit at zero through all of it. Matched to
 * `LiveUpdates`' own `NORMAL_POLL_MS` so an idle machine costs one more request on a
 * cadence the app already pays.
 */
export const IDLE_POLL_MS = 15_000;

/**
 * The freshness mechanism, and why it is not `LiveUpdates`.
 *
 * `LiveUpdates` (task-086) polls one **project's** revision, which is a hash of that
 * project's task files, and refetches an allowlist of project-scoped queries when it
 * moves. Neither half fits here. This answer is machine-wide, so no single project's
 * revision covers it; and most of what changes in it -- a session going quiet, a finish
 * reaching its gate, a run's elapsed seconds -- is not a task write at all, so even the
 * right project's revision would not move. The existing runs panel reached the same
 * conclusion for the same reason and polls on its own clock; this reuses that pattern
 * rather than inventing a second one.
 */
export function liveRunsPollInterval(body: LiveRunsView | undefined): number {
  if (!body) return BUSY_POLL_MS;
  return body.runs.length > 0 || body.holders.length > 0 ? BUSY_POLL_MS : IDLE_POLL_MS;
}

/** The machine-wide runs query, shared by the nav badge, the Dashboard and the tab. */
export function useLiveRuns() {
  const query = useQuery({
    ...listLiveRunsApiRunsLiveGetOptions(),
    refetchInterval: (state) => liveRunsPollInterval(state.state.data),
  });
  return query.data ?? null;
}

/**
 * The word a person reads for a run's state.
 *
 * `health`, never `status` and never `live`: "live" means only that nothing has declared
 * the run over, and rendering that as *working* is the specific mistake this surface
 * exists to avoid. A session parked on a permission prompt is live and is waiting for a
 * human; an orphaned batch run is live and is not running at all.
 */
export const HEALTH_LABELS: Record<string, string> = {
  working: "Working",
  starting: "Starting",
  parked: "Waiting on you",
  silent: "No output",
  orphaned: "Process gone",
  unknown: "Unreadable",
};

const HEALTH_CLASSES: Record<string, string> = {
  working: "bg-green-900 text-green-200",
  starting: "bg-slate-700 text-slate-200",
  parked: "bg-orange-900 text-orange-200",
  silent: "bg-orange-900 text-orange-200",
  orphaned: "bg-red-900 text-red-200",
  unknown: "bg-red-900 text-red-200",
};

export function healthLabel(health: string): string {
  return HEALTH_LABELS[health] ?? health;
}

export function HealthBadge({ health }: { health: string }) {
  return (
    <span
      data-health={health}
      className={`whitespace-nowrap rounded px-2 py-1 text-xs ${
        HEALTH_CLASSES[health] ?? HEALTH_CLASSES.unknown
      }`}
    >
      {healthLabel(health)}
    </span>
  );
}

/** "2 of 3 slots busy", or the honest version of it on an unconfigured machine. */
export function capacitySentence(body: LiveRunsView): string {
  if (!body.dispatch_configured) {
    return body.occupied > 0
      ? `${body.occupied} running`
      : "Dispatch is not configured on this machine";
  }
  return `${body.occupied} of ${body.max_concurrent_runs} ${
    body.max_concurrent_runs === 1 ? "slot" : "slots"
  } busy`;
}

/*
 * The Dashboard's capacity row lived here until task-092.
 *
 * It was one line at the foot of the statistics card saying "2 of 3 slots busy" and
 * naming what was running. The slot board says all of that in its own header, one card
 * higher and with the runs drawn rather than listed, so keeping the row would have been
 * the same sentence twice on the page that has to fit one viewport (task-294). Nothing
 * it offered was lost: the sentence is `capacitySentence`, which the board's header
 * renders, and the link to the Runs tab is the board's "Running now →".
 */

/** The nav badge. Always a number, so zero reads as zero rather than as stale. */
export function LiveRunCount({ body }: { body: LiveRunsView | null }) {
  const count = body?.runs.length ?? 0;
  return (
    <span
      data-testid="live-run-count"
      className={`ml-1.5 inline-flex min-w-5 justify-center rounded-full px-1.5 py-0.5 text-xs font-semibold ${
        count > 0 ? "bg-green-700 text-green-100" : "bg-dark-border text-dark-muted"
      }`}
    >
      {count}
    </span>
  );
}

function HolderRow({ holder }: { holder: MachineHolderView }) {
  const what =
    holder.kind === "runway"
      ? `Merge runway — ${holder.project_name || "a checkout"}`
      : `Finishing ${holder.task_id}${holder.task_title ? `: ${holder.task_title}` : ""}`;
  const body = (
    <>
      <span className="font-medium text-dark-text">{what}</span>
      <span className="text-dark-muted"> — {holder.detail}</span>
      {holder.elapsed_seconds !== null && holder.elapsed_seconds !== undefined && (
        <span className="text-dark-muted"> · {formatElapsed(holder.elapsed_seconds)}</span>
      )}
    </>
  );
  return (
    <li className="p-4 text-sm">
      {holder.task_url ? (
        <Link to={holder.task_url} className="hover:text-blue-300">
          {body}
        </Link>
      ) : (
        body
      )}
    </li>
  );
}

/**
 * The Runs tab.
 *
 * Read-only, deliberately (task-328's `out_of_scope`). There is no cancel here even
 * though the endpoint knows every run: a destructive button beside a row that a poll
 * refreshes every two seconds is a click aimed at whatever has since taken that row's
 * place, and task-312 records how badly a cancel at the wrong moment reads.
 */
export function LiveRunsPage({ body }: { body: LiveRunsView | null }) {
  if (!body) {
    return (
      <section className="rounded-lg border border-dark-border bg-dark-surface p-6">
        <h1 className="text-xl font-semibold">Running now</h1>
        <p className="mt-2 text-sm text-dark-muted">Reading the machine&apos;s run ledger…</p>
      </section>
    );
  }

  return (
    <div className="space-y-6">
      <section className="rounded-lg border border-dark-border bg-dark-surface p-6">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <h1 className="text-xl font-semibold">Running now</h1>
          <p className="text-sm text-dark-muted" data-testid="capacity-sentence">
            {capacitySentence(body)}
          </p>
        </div>
        <p className="mt-2 text-xs text-dark-muted">
          Every agent running on this machine, in every project — because the slots they
          compete for are the machine&apos;s, not any one project&apos;s. Read-only: start
          and cancel a run from its own task.
        </p>
      </section>

      <section className="rounded-lg border border-dark-border bg-dark-surface">
        <div className="border-b border-dark-border p-6">
          <h2 className="text-lg font-semibold">Runs</h2>
        </div>
        {body.runs.length === 0 ? (
          <p className="p-6 text-sm text-dark-muted" data-testid="no-live-runs">
            Nothing is running on this machine right now.
          </p>
        ) : (
          <div className="p-2">
            <ResponsiveTable aria-label="Live runs">
              <thead>
                <tr>
                  <th scope="col">Task</th>
                  <th scope="col">Project</th>
                  <th scope="col">State</th>
                  <th scope="col">Running for</th>
                  <th scope="col">Posture</th>
                </tr>
              </thead>
              <tbody>
                {body.runs.map((run) => (
                  <ResponsiveTableRow key={run.run_id} data-run-id={run.run_id}>
                    <ResponsiveCell label="Task">
                      {/* Absolute, and built by the server: this row very often belongs
                          to another project, and a relative link would send it into
                          whichever project the reader is currently in. */}
                      <Link to={run.task_url} className="text-blue-400 hover:text-blue-300">
                        <span className="font-mono text-xs">{run.task_id}</span>
                        <span className="ml-2 text-sm text-dark-text">{run.task_title}</span>
                      </Link>
                    </ResponsiveCell>
                    <ResponsiveCell label="Project" className="text-sm text-dark-muted">
                      {run.project_name}
                    </ResponsiveCell>
                    <ResponsiveCell label="State">
                      <HealthBadge health={run.health} />
                    </ResponsiveCell>
                    <ResponsiveCell label="Running for" className="text-sm text-dark-muted">
                      {formatElapsed(run.elapsed_seconds)}
                    </ResponsiveCell>
                    <ResponsiveCell label="Posture" className="text-xs text-dark-muted">
                      {run.posture}
                      {run.session ? "" : " · batch"}
                    </ResponsiveCell>
                  </ResponsiveTableRow>
                ))}
              </tbody>
            </ResponsiveTable>
          </div>
        )}
      </section>

      {body.holders.length > 0 && (
        <section className="rounded-lg border border-dark-border bg-dark-surface">
          <div className="border-b border-dark-border p-6">
            <h2 className="text-lg font-semibold">Also on this machine</h2>
            <p className="mt-1 text-xs text-dark-muted">
              Merges in progress. They hold locks rather than run slots, so they are not
              in the count above — but a task cannot be dispatched while one holds it,
              and a repository&apos;s merges queue behind its runway.
            </p>
          </div>
          <ul className="divide-y divide-dark-border">
            {body.holders.map((holder) => (
              <HolderRow key={`${holder.kind}-${holder.lock_name}`} holder={holder} />
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}
