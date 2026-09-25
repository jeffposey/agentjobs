import { useQuery } from "@tanstack/react-query";

import { listLiveRunsApiRunsLiveGetOptions } from "../api/generated/@tanstack/react-query.gen";
import type { LiveRunView, LiveRunsView, MachineHolderView } from "../api/types";
import { CHIP_SHAPE, RUN_HEALTH, categoryStyle, chipClasses, runMotion } from "./StatusChip";

/**
 * What is running on this machine, across every project (task-328).
 *
 * Two surfaces, one query, on purpose. The Dashboard tab's working and landing counts and the
 * Dashboard's slot board render the same answer at different lengths, and react-query
 * dedupes them by key -- so a Dashboard with the header above it costs one request.
 *
 * **There was a third surface, the Runs tab, and task-588 retired it**: the owner never
 * opened it, and its one number moved onto the Dashboard tab. What it rendered and the
 * slot board does not -- other projects' runs as rows -- is still in this answer, since
 * every row carries its own `project_id` and server-built `task_url`, so a future surface
 * that wants it needs no new endpoint. `/p/:projectId/runs` redirects to the Dashboard.
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
  // A waiting dispatch counts as busy (task-459). The machine is full by definition
  // while one is queued, and the moment worth seeing promptly is the one where a slot
  // frees and the card turns into a run.
  // A walk that is still taking off counts as busy too (task-523): it starts children
  // with nobody clicking anything, so the moment worth seeing promptly is the one where
  // a slot fills with work nobody asked for by hand. A *grounded* walk is deliberately
  // not busy -- one waiting on a review can wait for days, and a page polling every two
  // seconds for it would be paying a busy machine's cost for an idle one.
  const busy =
    body.runs.length > 0 ||
    body.holders.length > 0 ||
    (body.queued?.length ?? 0) > 0 ||
    (body.walks ?? []).some((walk) => !walk.grounded);
  return busy ? BUSY_POLL_MS : IDLE_POLL_MS;
}

/** The machine-wide runs query, shared by the header readout and the slot board. */
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
const PROCESS_HEALTH_LABELS: Record<string, string> = {
  silent: "No output",
  orphaned: "Process gone",
  unknown: "Unreadable",
  // An interactive session whose transcript has not changed for a while (task-354).
  // Not an alarm: a chat window left open is the most ordinary state there is.
  idle: "Idle",
  // The task this run was dispatched for is closed, and the session is still open
  // (task-482). The badge draws the task's own word instead (task-577), so this is only
  // the fallback for a task that could not be read.
  work_done: "Task closed",
};

/**
 * Every health word. The ones that name the same thing as a task status -- Working,
 * Starting, Waiting on you, Landing, and Feedback (a human handed the ball back while
 * this run was still going, task-384) -- come with their category from the status data
 * file, so a run and its task are drawn in one word and one colour (task-562). The rest
 * describe the process rather than the task and are spelled here.
 */
export const HEALTH_LABELS: Record<string, string> = {
  ...PROCESS_HEALTH_LABELS,
  ...Object.fromEntries(Object.entries(RUN_HEALTH).map(([health, entry]) => [health, entry.label])),
};

/**
 * The rest describe the process rather than the task, and keep colours of their own.
 * Same chip shape as a status, so the board does not mix filled and outlined badges.
 */
const HEALTH_CLASSES: Record<string, string> = {
  silent: "border-orange-700 bg-orange-900 text-orange-200",
  orphaned: "border-red-700 bg-red-900 text-red-200",
  unknown: "border-red-700 bg-red-900 text-red-200",
  idle: "border-slate-600 bg-slate-700 text-slate-200",
  work_done: "border-slate-600 bg-slate-700 text-slate-200",
};

/**
 * What kind of run this is, in one word, where a dispatched run shows its posture.
 *
 * An interactive run has no posture -- AgentJobs did not choose that session's
 * permission envelope and cannot read it (task-354) -- so printing the field would print
 * an empty cell. "You, in a chat window" is what the reader needs instead.
 *
 * A walk run has a posture and no agent: it handed an epic's children to the server and
 * ended (task-458). Printing its posture would say what its *children* run at, over a row
 * that is not running anything, so it is named for what it is instead.
 */
export function runKindLabel(run: LiveRunView): string {
  if (run.mode === "interactive") return "your session";
  if (run.mode === "walk") return "epic walk · no agent";
  return `${run.posture}${run.session ? "" : " · batch"}`;
}

export function healthLabel(health: string): string {
  return HEALTH_LABELS[health] ?? health;
}

/** The closed task's own chip, which a `work_done` run carries so it can say the same word. */
type ClosedTask = Pick<LiveRunView, "task_display_status" | "task_status_category">;

export function HealthBadge({ health, task }: { health: string; task?: ClosedTask }) {
  // A run whose task has closed says what its task says -- "Completed", in closed grey --
  // rather than a word of its own beside it (task-577). The server sends the word only
  // for that health, so a stale or open task never reaches this branch.
  if (health === "work_done" && task?.task_display_status && task.task_status_category) {
    return (
      <span
        data-health={health}
        data-status-category={task.task_status_category}
        className={CHIP_SHAPE}
        style={categoryStyle(task.task_status_category)}
      >
        {task.task_display_status}
      </span>
    );
  }
  const status = RUN_HEALTH[health];
  if (status) {
    return (
      <span
        data-health={health}
        data-status-category={status.category}
        data-motion={runMotion(health) ?? undefined}
        className={chipClasses(runMotion(health))}
        style={categoryStyle(status.category)}
      >
        {status.label}
      </span>
    );
  }
  return (
    <span data-health={health} className={`${CHIP_SHAPE} ${HEALTH_CLASSES[health] ?? HEALTH_CLASSES.unknown}`}>
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
  // A run finishing itself explains its runway too: it keeps its own lock rather than
  // taking a finish one, so its finish id is on the run row (task-533).
  const listed = new Set(
    [
      ...liveFinishes(body).map((finish) => finish.finish_id ?? ""),
      ...body.runs.map((run) => run.finish_id ?? ""),
    ].filter((id) => id.length > 0),
  );
  return body.holders.filter(
    (holder) => holder.kind === "runway" && !(holder.finish_id && listed.has(holder.finish_id)),
  );
}

/**
 * How many merges are landing: the same classification the slot board draws as Landing.
 *
 * Two shapes, never both for one merge. A finish started by an approval is a finish
 * holder and its run is gone; a run finishing itself keeps its run row, reports
 * `health: "finishing"`, and takes no finish lock (task-533). An overtaken finish is not
 * landing -- its task is already closed, and the board badges it Overtaken (task-514).
 * `capacitySentence` and the Dashboard tab both count through here, so the sentence, the
 * board and the tab cannot disagree about what is merging.
 */
export function landingCount(body: LiveRunsView): number {
  return (
    liveFinishes(body).filter((finish) => !finish.overtaken).length +
    body.runs.filter((run) => run.health === "finishing").length
  );
}

/**
 * The Dashboard tab's machine-wide counts: runs being worked, and merges landing.
 *
 * Disjoint by construction (task-608). Until then the tab's one count was runs plus
 * finishes, and a run finishing itself sat in it as a run; now it is landing and not
 * working, so a landing that fails back to a working run moves from one count to the
 * other on the next poll rather than being counted twice or dropped. An overtaken finish
 * is in neither: nothing is being worked on a closed task, and nothing is landing.
 */
export function runCounts(body: LiveRunsView | null): { working: number; landing: number } {
  if (!body) return { working: 0, landing: 0 };
  const working = body.runs.filter((run) => run.health !== "finishing").length;
  return { working, landing: landingCount(body) };
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
  teardown: "Stopping review sandboxes",
  worktree: "Removing the worktree",
  branch: "Deleting the branch",
  // The server's word for a finish whose record it could not read: the lock is held,
  // the step is unknown.
  merging: "Merging",
  // task-514: the lock is held by a finish that merged nothing, on a task already closed.
  overtaken: "Task already closed",
};

export function finishStepLabel(detail: string | undefined): string {
  if (!detail) return "Merging";
  return FINISH_STEP_LABELS[detail] ?? detail;
}

/**
 * The step, plus whose merge it is waiting behind when it is queued for the runway.
 *
 * "Queued for the merge runway" alone read as a finish going nowhere, while the runway
 * holder beside it named no task at all (task-533, seen 2026-09-23). The chip stays one
 * word; the detail line is where the queue is explained.
 */
export function finishDetail(step: string | undefined, behind: string | undefined): string {
  const label = finishStepLabel(step);
  return behind ? `${label}, behind ${behind}` : label;
}

/**
 * The state word for a finish, styled apart from a run's health: it is not a session.
 *
 * `overtaken` is the one that is not "Finishing" (task-514). This board renders from the
 * *lock*, so a finish that merged nothing and is holding a task somebody else already
 * finished read as Finishing for twenty minutes beside a task page reading Completed.
 * The lock is real and stays on the board; the word for it is not.
 */
export function FinishBadge({ finish }: { finish: MachineHolderView }) {
  return (
    <span
      data-finish-step={finish.detail}
      data-status-category={finish.overtaken ? undefined : "finishing"}
      // A lock state rather than a task status, so Overtaken keeps a colour of its own.
      data-motion={finish.overtaken ? undefined : (runMotion("finishing") ?? undefined)}
      className={finish.overtaken ? `${CHIP_SHAPE} border-orange-700 bg-orange-900 text-orange-200` : chipClasses(runMotion("finishing"))}
      style={finish.overtaken ? undefined : categoryStyle("finishing")}
    >
      {finish.overtaken ? "Overtaken" : RUN_HEALTH.finishing?.label}
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
  // Overtaken finishes are excluded rather than counted: they hold a lock and a slot on
  // the board, but "1 merging" is the word this sentence exists to make honest (task-514).
  // A run finishing itself is merging too, and holds no finish card (task-533).
  const merging = landingCount(body);
  const suffix = merging > 0 ? ` · ${merging} merging` : "";
  if (!body.dispatch_configured) {
    const head =
      body.occupied > 0 ? `${body.occupied} running` : "Dispatch is not configured on this machine";
    return `${head}${suffix}`;
  }
  // "3 of 2 slots busy" is the honest sentence for a machine somebody chose to run over
  // its ceiling (task-461), and clamping it to "2 of 2" would be the surface lying to
  // keep a number tidy. The clause is what stops it reading as a counting bug.
  const over = Math.max(0, body.occupied - body.max_concurrent_runs);
  const overage = over > 0 ? ` · ${over} over the ceiling` : "";
  return `${body.occupied} of ${body.max_concurrent_runs} ${
    body.max_concurrent_runs === 1 ? "slot" : "slots"
  } busy${overage}${suffix}`;
}
