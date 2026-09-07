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
  // An interactive session whose transcript has not changed for a while (task-354).
  // Not an alarm: a chat window left open is the most ordinary state there is.
  idle: "Idle",
  // A human moved the ball back to the agent while this run was still going (task-384).
  // Ahead of "Working" in `run_health` because both are true and this is the one the
  // reader is asking about: the click landed, and it is queued for this session.
  handback: "Feedback waiting",
};

const HEALTH_CLASSES: Record<string, string> = {
  working: "bg-green-900 text-green-200",
  starting: "bg-slate-700 text-slate-200",
  parked: "bg-orange-900 text-orange-200",
  silent: "bg-orange-900 text-orange-200",
  orphaned: "bg-red-900 text-red-200",
  unknown: "bg-red-900 text-red-200",
  idle: "bg-slate-700 text-slate-200",
  handback: "bg-sky-900 text-sky-200",
};

/**
 * What kind of run this is, in one word, where a dispatched run shows its posture.
 *
 * An interactive run has no posture -- AgentJobs did not choose that session's
 * permission envelope and cannot read it (task-354) -- so printing the field would print
 * an empty cell. "You, in a chat window" is what the reader needs instead.
 */
export function runKindLabel(run: LiveRunView): string {
  if (run.mode === "interactive") return "your session";
  return `${run.posture}${run.session ? "" : " · batch"}`;
}

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

/**
 * The finishes in progress, which every surface here counts and lists as running
 * (task-352).
 *
 * A scripted finish holds locks rather than a run slot, which is why the server leaves
 * it out of `occupied` and must go on doing so: that number is the concurrency guard's,
 * and a browser that disagreed with it would offer a Dispatch button the server refuses.
 * But the question this surface exists to answer is "what is running on this machine",
 * and a finish spending five minutes in the gate for a task is the plain answer to it.
 * task-092's own finish sat under a heading saying nothing was running, with the badge
 * at zero, and that is how task-352 came to be filed.
 *
 * So the split is: `occupied` is slots, and stays the server's word; what is *running*
 * is the runs plus these. The runway holder is never a second item -- it is the same
 * finish's lock on the repository, and a finish that holds it is counted once.
 */
export function liveFinishes(body: LiveRunsView): MachineHolderView[] {
  return body.holders.filter((holder) => holder.kind === "finish");
}

/**
 * Runway locks whose finish is not otherwise listed.
 *
 * Almost always empty: a finish takes its task lock before the runway, so a runway in
 * hand means a finish row above it. The exception is a runway whose holder's finish
 * record cannot be read, and that one is still worth a line -- every other merge in
 * that repository is queued behind it.
 */
export function unexplainedRunways(body: LiveRunsView): MachineHolderView[] {
  const listed = new Set(
    liveFinishes(body)
      .map((finish) => finish.finish_id ?? "")
      .filter((id) => id.length > 0),
  );
  return body.holders.filter(
    (holder) => holder.kind === "runway" && !(holder.finish_id && listed.has(holder.finish_id)),
  );
}

/** What the badge counts: dispatched runs plus finishes in progress. */
export function runningCount(body: LiveRunsView | null): number {
  if (!body) return 0;
  return body.runs.length + liveFinishes(body).length;
}

/**
 * The finish's step, in words for somebody watching their task merge.
 *
 * `detail` is the step name from the finish's own vocabulary -- `gate`, `runway` -- and
 * "runway" means nothing to a person reading a table. These are shorter than the
 * sentences `dispatch/finish_status.py` keeps for the same steps, because they render
 * in a table cell and on a card with four lines of room.
 */
export const FINISH_STEP_LABELS: Record<string, string> = {
  preflight: "Checking the branch",
  runway: "Queued for the merge runway",
  rebase: "Rebasing onto main",
  gate: "Running the gate",
  catch_up: "Re-verifying after main moved",
  merge: "Merging",
  rebuild: "Rebuilding the frontend",
  restart: "Restarting the server",
  verify: "Checking the merge is live",
  close: "Closing the task",
  worktree: "Removing the worktree",
  branch: "Deleting the branch",
  // The server's word for a finish whose record it could not read: the lock is held,
  // the step is unknown.
  merging: "Merging",
};

export function finishStepLabel(detail: string | undefined): string {
  if (!detail) return "Merging";
  return FINISH_STEP_LABELS[detail] ?? detail;
}

/** The state word for a finish, styled apart from a run's health: it is not a session. */
export function FinishBadge({ finish }: { finish: MachineHolderView }) {
  return (
    <span
      data-finish-step={finish.detail}
      className="whitespace-nowrap rounded bg-violet-900 px-2 py-1 text-xs text-violet-200"
    >
      Finishing
    </span>
  );
}

/**
 * "2 of 3 slots busy", or the honest version of it on an unconfigured machine.
 *
 * With a finish in progress the sentence says so -- "0 of 3 slots busy · 1 merging" --
 * because the slot count on its own, read beside a five-minute gate, is the sentence
 * that made task-092's finish look like nothing was happening (task-352).
 */
export function capacitySentence(body: LiveRunsView): string {
  const merging = liveFinishes(body).length;
  const suffix = merging > 0 ? ` · ${merging} merging` : "";
  if (!body.dispatch_configured) {
    const head =
      body.occupied > 0 ? `${body.occupied} running` : "Dispatch is not configured on this machine";
    return `${head}${suffix}`;
  }
  return `${body.occupied} of ${body.max_concurrent_runs} ${
    body.max_concurrent_runs === 1 ? "slot" : "slots"
  } busy${suffix}`;
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

/**
 * The nav badge. Always a number, so zero reads as zero rather than as stale.
 *
 * It counts what is running, not what holds a slot: a finish in the gate is a one here
 * (task-352). The slot count is the capacity sentence's job.
 */
export function LiveRunCount({ body }: { body: LiveRunsView | null }) {
  const count = runningCount(body);
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

/** A runway lock with no finish row to explain it. See `unexplainedRunways`. */
function RunwayRow({ holder }: { holder: MachineHolderView }) {
  return (
    <li className="p-4 text-sm">
      <span className="font-medium text-dark-text">
        Merge runway — {holder.project_name || "a checkout"}
      </span>
      <span className="text-dark-muted"> — {holder.detail}</span>
      {holder.elapsed_seconds !== null && holder.elapsed_seconds !== undefined && (
        <span className="text-dark-muted"> · {formatElapsed(holder.elapsed_seconds)}</span>
      )}
    </li>
  );
}

/**
 * A finish, as a row of the same table the runs are in.
 *
 * The Task cell links where a run's does, and falls back to plain text for a finish
 * whose project the server could not attribute: the row is still worth having, and a
 * link to nowhere is worse than no link.
 */
function FinishRow({ finish }: { finish: MachineHolderView }) {
  const name = (
    <>
      <span className="font-mono text-xs">{finish.task_id || finish.lock_name}</span>
      {finish.task_title && (
        <span className="ml-2 text-sm text-dark-text">{finish.task_title}</span>
      )}
    </>
  );
  return (
    <ResponsiveTableRow
      data-finish-id={finish.finish_id}
      data-task-id={finish.task_id}
    >
      <ResponsiveCell label="Task">
        {finish.task_url ? (
          <Link to={finish.task_url} className="text-blue-400 hover:text-blue-300">
            {name}
          </Link>
        ) : (
          <span className="text-dark-text">{name}</span>
        )}
      </ResponsiveCell>
      <ResponsiveCell label="Project" className="text-sm text-dark-muted">
        {finish.project_name}
      </ResponsiveCell>
      <ResponsiveCell label="State">
        <span className="inline-flex flex-wrap items-center gap-2">
          <FinishBadge finish={finish} />
          <span className="text-xs text-dark-muted">{finishStepLabel(finish.detail)}</span>
        </span>
      </ResponsiveCell>
      <ResponsiveCell label="Running for" className="text-sm text-dark-muted">
        {formatElapsed(finish.elapsed_seconds)}
      </ResponsiveCell>
      <ResponsiveCell label="Posture" className="text-xs text-dark-muted">
        scripted finish
      </ResponsiveCell>
    </ResponsiveTableRow>
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

  const finishes = liveFinishes(body);
  const runways = unexplainedRunways(body);

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
        {body.runs.length === 0 && finishes.length === 0 ? (
          <p className="p-6 text-sm text-dark-muted" data-testid="no-live-runs">
            Nothing is running on this machine right now.
          </p>
        ) : (
          <div className="p-2">
            {/* Task, Project, State, Running for, Posture. Only the first holds a task
                title, so only the first is unbounded; the rest are a project name, a
                badge, an elapsed time and a word. */}
            <ResponsiveTable aria-label="Live runs" columns={[null, "10rem", "8rem", "8rem", "9rem"]}>
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
                      {runKindLabel(run)}
                    </ResponsiveCell>
                  </ResponsiveTableRow>
                ))}
                {/* Finishes after the runs: a finish is what happens to a task once
                    its run is over, so it reads as the later thing. It holds no slot,
                    which the capacity sentence above already says. */}
                {finishes.map((finish) => (
                  <FinishRow key={`finish-${finish.lock_name}`} finish={finish} />
                ))}
              </tbody>
            </ResponsiveTable>
          </div>
        )}
      </section>

      {runways.length > 0 && (
        <section className="rounded-lg border border-dark-border bg-dark-surface">
          <div className="border-b border-dark-border p-6">
            <h2 className="text-lg font-semibold">Also on this machine</h2>
            <p className="mt-1 text-xs text-dark-muted">
              A merge runway held by a finish this page could not read. Every other merge
              in that repository is queued behind it.
            </p>
          </div>
          <ul className="divide-y divide-dark-border">
            {runways.map((holder) => (
              <RunwayRow key={`runway-${holder.lock_name}`} holder={holder} />
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}
