import { Link } from "react-router-dom";

import type { LiveRunView, LiveRunsView, MachineHolderView, TaskRead } from "../api/types";
import { formatElapsed } from "./DispatchPanel";
import {
  FinishBadge,
  HealthBadge,
  capacitySentence,
  finishStepLabel,
  liveFinishes,
  runKindLabel,
  unexplainedRunways,
} from "./LiveRuns";

/**
 * The Dashboard as a board of run slots (task-092).
 *
 * One cell per `max_concurrent_runs`, each holding either the run occupying that slot
 * or the next queued task with a Dispatch button. The ladder it replaces ranked
 * *panels* and showed the winner, which meant the page had exactly one position for a
 * call to action and every calm rung competed for it. A board ranks nothing: it draws
 * the machine's capacity as a fixed number of cells and fills each one with whatever is
 * true of it, so the competition disappears rather than being adjudicated.
 *
 * Jeff, 2026-09-05: "the dashboard should show the X max concurrent number of runs,
 * either with next task and info and dispatch button, or the active run using that
 * slot."
 *
 * **It is not a rung of the ladder.** The ladder still runs, still strict, and still
 * picks exactly one call to action; the board is the machine's shape and shows beside
 * whichever rung won. On an alarming day it moves below the alarm and goes
 * `statusOnly` -- see that prop for why that is position and silence rather than
 * suppression.
 */

/**
 * The most cells the board will draw, whatever the ceiling says.
 *
 * Six. The bound exists because `max_concurrent_runs` is a number a person can now type
 * (task-344) and the Dashboard has to fit one viewport (task-294) -- at two columns on
 * a 820px screen six cells is three rows, which is the last count that still leaves room
 * for the sections under it on a phone. Past six the board draws six and *says* what it
 * is not drawing, rather than growing a scroll into the one page whose whole point is
 * not scrolling.
 *
 * A ceiling above six is also a number worth stating rather than illustrating: this
 * machine's gate already divides its cores between the runs it can see (task-339), so
 * the seventh slot is not seven agents working, it is seven agents queueing.
 */
export const BOARD_CELL_LIMIT = 6;

/**
 * What one cell is showing.
 *
 * `opaque` is the cell that only a machine-wide view can have: `occupied` counts every
 * run on the machine including ones in projects this caller may not see, while `runs`
 * lists only the visible ones (task-333). The slot is genuinely taken, so drawing it
 * free -- with a Dispatch button the server would refuse -- would make the browser the
 * one place in the system able to disagree with the concurrency guard.
 *
 * `finish` is a card for a scripted finish in progress (task-352). It is drawn in the
 * board because a finish running a five-minute gate is this machine working on a task,
 * and a board that said "0 of 3 slots busy" over three free cells while it ran was read
 * as nothing happening. It holds no slot, so it is *added* to the board rather than
 * taking a cell from it: the free cells and their Dispatch buttons are exactly what
 * they would be without it.
 *
 * An **interactive** run -- a session somebody is working the task in (task-354) -- is
 * a `run` cell like any other and is added the same way, for the same reason: it is in
 * `runs` and not in `occupied`, because it holds its task rather than a slot.
 */
export type SlotCell =
  | { kind: "run"; key: string; run: LiveRunView }
  | { kind: "opaque"; key: string }
  | { kind: "finish"; key: string; finish: MachineHolderView }
  | { kind: "queued"; key: string; task: TaskRead }
  | { kind: "empty"; key: string };

export type BoardLayout = {
  cells: SlotCell[];
  /** Slots the ceiling claims that {@link BOARD_CELL_LIMIT} refused to draw. */
  hiddenSlots: number;
  /** How many of those are busy, so the footer can say the board is not the whole truth. */
  hiddenBusy: number;
  /** A ceiling nobody chose: the board degrades to a queue rather than asserting a shape. */
  unconfigured: boolean;
};

/**
 * Occupied cells first, in start order; free cells after, in queue order.
 *
 * **There is no such thing as slot 3.** `live_runs` answers with a list and nothing in
 * AgentJobs allocates, numbers or identifies a slot, so the board's cells are a fiction
 * this function maintains -- and the only thing that makes them usable is that the
 * fiction is stable. Start order is what gives that: a run that begins is appended
 * after every run already going, and a run that ends is removed while the rest keep
 * their order, so no poll can permute two cells that both did nothing. Sorting by
 * anything the poll recomputes -- health, elapsed, the ledger's own scan order -- would
 * shuffle the board every two seconds.
 *
 * `run_id` breaks ties so two runs started in the same millisecond still have an order,
 * and a run with no `started_at` sorts last rather than randomly.
 */
export function orderedRuns(runs: LiveRunView[]): LiveRunView[] {
  return [...runs].sort((left, right) => {
    const a = left.started_at ?? "";
    const b = right.started_at ?? "";
    if (a !== b) {
      if (!a) return 1;
      if (!b) return -1;
      return a < b ? -1 : 1;
    }
    return left.run_id < right.run_id ? -1 : left.run_id > right.run_id ? 1 : 0;
  });
}

/** The same stability rule for finish cards: start order, lock name breaking ties. */
export function orderedFinishes(finishes: MachineHolderView[]): MachineHolderView[] {
  return [...finishes].sort((left, right) => {
    const a = left.started_at;
    const b = right.started_at;
    if (a !== b) {
      if (!a) return 1;
      if (!b) return -1;
      return a < b ? -1 : 1;
    }
    return left.lock_name < right.lock_name ? -1 : left.lock_name > right.lock_name ? 1 : 0;
  });
}

/**
 * Turn one machine answer and one project queue into the cells to draw.
 *
 * Pure, and separate from the rendering, because every rule worth arguing about is in
 * here: how many cells there are, which are busy, and what fills the rest.
 */
export function boardLayout(
  body: LiveRunsView,
  queue: TaskRead[],
  projectId: string,
): BoardLayout {
  const unconfigured = !body.dispatch_configured;
  // Slot cells are filled from the runs that *hold* slots. An interactive session is in
  // `runs` and not in `occupied` (task-354), so indexing the slot cells into the whole
  // list would put a chat session in a slot cell and push a dispatched run out of one.
  const slotted = orderedRuns(body.runs.filter((run) => run.mode !== "interactive"));
  const attended = orderedRuns(body.runs.filter((run) => run.mode === "interactive"));
  // `occupied`, never `runs.length`. The count is the machine's and the list is this
  // caller's, and the cell that exists for the difference is `opaque`.
  const slots = unconfigured
    ? Math.min(body.occupied, BOARD_CELL_LIMIT)
    : Math.min(body.max_concurrent_runs, BOARD_CELL_LIMIT);
  const busy = Math.min(body.occupied, slots);

  const cells: SlotCell[] = [];
  for (let index = 0; index < busy; index += 1) {
    const run = slotted[index];
    cells.push(
      run ? { kind: "run", key: run.run_id, run } : { kind: "opaque", key: `opaque-${index}` },
    );
  }

  // Then the things that are running and hold no slot, in the order a person meets
  // them: a session working the task, then the merge that follows it. Neither is
  // counted against `slots` -- `free` below is computed from `busy` alone -- so a board
  // with either on it has more cards than the ceiling, and that is the honest shape:
  // three slots, none taken, and work happening beside them.
  for (const run of attended) {
    cells.push({ kind: "run", key: run.run_id, run });
  }
  const finishes = orderedFinishes(liveFinishes(body));
  for (const finish of finishes) {
    cells.push({ kind: "finish", key: `finish-${finish.lock_name}`, finish });
  }

  // A task already holding a cell is not offered a second one. The two lists come from
  // two endpoints and nothing reconciles them: a run whose task was released back to
  // `ready` while its process is still alive is live *and* claimable, and the board
  // would then show it twice with a Dispatch button under the second copy. Scoped to
  // this project, because a task id is only unique within one -- `task-101` here and
  // `task-101` elsewhere are two different tasks and only one of them is on this queue.
  // A task being finished is held the same way: its finish card is its cell.
  const running = new Set([
    ...body.runs.filter((item) => item.project_id === projectId).map((item) => item.task_id),
    ...finishes.filter((item) => item.project_id === projectId).map((item) => item.task_id),
  ]);
  const offerable = queue.filter((task) => !running.has(task.id));

  // A free cell per remaining slot, each offering a *different* task. When the queue is
  // shorter than the free slots the rest are `empty`: the honest answer there is a slot
  // with nothing to put in it, not the same task repeated into three cells.
  const free = unconfigured
    ? Math.min(offerable.length, BOARD_CELL_LIMIT)
    : Math.max(0, slots - busy);
  for (let index = 0; index < free; index += 1) {
    const task = offerable[index];
    cells.push(
      task ? { kind: "queued", key: task.id, task } : { kind: "empty", key: `empty-${index}` },
    );
  }

  const ceiling = unconfigured ? body.occupied : body.max_concurrent_runs;
  return {
    cells,
    hiddenSlots: Math.max(0, ceiling - slots),
    hiddenBusy: Math.max(0, body.occupied - busy),
    unconfigured,
  };
}

/**
 * How many columns the grid gets, given how many cells there are.
 *
 * One column on a phone, two from the app's own 820px breakpoint, three from 1024px --
 * three early on purpose, because the common ceiling on a workstation is three and at
 * two columns that is a row of two above a ragged row of one, which reads as a board
 * with a hole in it rather than as the machine's shape.
 *
 * **Capped at the cell count**, which is the part that is not obvious: a machine with
 * `max_concurrent_runs: 1` drew a single card occupying a third of the panel with two
 * thirds of empty card beside it. A one-slot board is a panel, and a panel is full
 * width. The three strings are written out rather than composed because Tailwind scans
 * for whole class names and would not see one this function built.
 */
export function boardColumns(cells: number): string {
  if (cells <= 1) return "grid-cols-1";
  if (cells === 2) return "grid-cols-1 min-[820px]:grid-cols-2";
  return "grid-cols-1 min-[820px]:grid-cols-2 min-[1024px]:grid-cols-3";
}

function projectPath(projectId: string, path = "") {
  return `/p/${encodeURIComponent(projectId)}${path}`;
}

function truncate(text: string, limit: number) {
  const compact = text.replace(/\s+/g, " ").trim();
  return compact.length <= limit ? compact : `${compact.slice(0, limit - 1)}…`;
}

/**
 * The shape every cell shares.
 *
 * `h-full` is load-bearing: a grid row is as tall as its tallest cell, and without it a
 * free cell with a Dispatch button in it leaves the runs beside it floating in a short
 * box with a gap underneath. The board should read as one row of equal slots, because
 * that is what it is.
 */
const CELL_BASE =
  "flex h-full min-h-[7.5rem] flex-col justify-between gap-2 rounded-lg border p-3 text-left";

function RunCell({ run, projectId }: { run: LiveRunView; projectId: string }) {
  // The project name only where it is news. Every occupied cell may belong to another
  // project -- slots are the machine's -- but printing "agentjobs" on the agentjobs
  // dashboard is a line of noise on a card with four lines of room.
  const elsewhere = Boolean(run.project_id) && run.project_id !== projectId;
  return (
    <div className={`${CELL_BASE} border-dark-border bg-dark-bg`}>
      <div className="min-w-0">
        <div className="mb-1 flex items-center justify-between gap-2">
          <HealthBadge health={run.health} />
          <span className="shrink-0 text-xs text-dark-muted">
            {formatElapsed(run.elapsed_seconds)}
          </span>
        </div>
        {/* Absolute and server-built: a row for another project must link into that
            project, not into whichever one the reader happens to be looking at. */}
        <Link to={run.task_url} className="block min-w-0 hover:text-blue-300">
          <div className="truncate font-mono text-xs text-blue-400">{run.task_id}</div>
          <div className="line-clamp-2 text-sm font-medium text-dark-text">
            {run.task_title || run.run_id}
          </div>
        </Link>
      </div>
      <div className="flex items-center justify-between gap-2 text-xs text-dark-muted">
        <span className="truncate">{elsewhere ? run.project_name : runKindLabel(run)}</span>
        {elsewhere && <span className="shrink-0">{runKindLabel(run)}</span>}
      </div>
    </div>
  );
}

/**
 * A scripted finish, drawn as a card of the board (task-352).
 *
 * Same shape as a run cell: state and elapsed on the first line, the task linked
 * beneath, and the foot saying what it is -- the finish step, in words, where a run
 * shows its posture. The step is the one thing a person watching a merge wants to know
 * and the one thing the footnote this replaced did not say.
 */
function FinishCell({ finish, projectId }: { finish: MachineHolderView; projectId: string }) {
  const elsewhere = Boolean(finish.project_id) && finish.project_id !== projectId;
  const name = (
    <>
      <div className="truncate font-mono text-xs text-blue-400">
        {finish.task_id || finish.lock_name}
      </div>
      <div className="line-clamp-2 text-sm font-medium text-dark-text">
        {finish.task_title || finish.finish_id || "A merge in progress"}
      </div>
    </>
  );
  return (
    <div className={`${CELL_BASE} border-violet-900/70 bg-dark-bg`}>
      <div className="min-w-0">
        <div className="mb-1 flex items-center justify-between gap-2">
          <FinishBadge finish={finish} />
          <span className="shrink-0 text-xs text-dark-muted">
            {formatElapsed(finish.elapsed_seconds)}
          </span>
        </div>
        {finish.task_url ? (
          <Link to={finish.task_url} className="block min-w-0 hover:text-blue-300">
            {name}
          </Link>
        ) : (
          <div className="min-w-0">{name}</div>
        )}
      </div>
      <div className="flex items-center justify-between gap-2 text-xs text-dark-muted">
        <span className="truncate">{finishStepLabel(finish.detail)}</span>
        {elsewhere && <span className="shrink-0">{finish.project_name}</span>}
      </div>
    </div>
  );
}

function OpaqueCell() {
  return (
    <div className={`${CELL_BASE} border-dark-border bg-dark-bg`}>
      <span className="rounded bg-slate-700 px-2 py-1 text-xs text-slate-200">Busy</span>
      <p className="text-xs text-dark-muted">
        A run in a project this view cannot show. The slot is taken either way, which is
        why it is not offered.
      </p>
    </div>
  );
}

function QueuedCell({
  task,
  projectId,
  label,
  action,
  disclosure,
}: {
  task: TaskRead;
  projectId: string;
  /** "Slot free", or "Next up" on a machine whose slots are a number nobody chose. */
  label: string;
  action?: React.ReactNode;
  disclosure?: React.ReactNode;
}) {
  return (
    <div className={`${CELL_BASE} border-dashed border-sky-800/70 bg-dark-bg`}>
      <div className="min-w-0">
        <div className="mb-1 flex items-center justify-between gap-2">
          <span className="text-xs uppercase tracking-wide text-dark-muted">{label}</span>
          <span className="shrink-0 text-xs text-dark-muted">{task.priority ?? "medium"}</span>
        </div>
        <Link
          to={projectPath(projectId, `/tasks/${encodeURIComponent(task.id)}`)}
          className="block min-w-0 hover:text-blue-300"
        >
          <div className="truncate font-mono text-xs text-blue-400">{task.id}</div>
          <div className="line-clamp-2 text-sm font-medium text-dark-text">{task.title}</div>
          {/*
            "Enough of its spec to judge it" is what a free cell is for (task-092), so
            the summary stays at every width. Hiding it below 820px was tried and
            measured: a three-cell board on a 390x844 phone went from 672px to 636px,
            which is not worth the line a person picks the task by.
          */}
          <p className="mt-1 line-clamp-2 text-xs text-dark-muted">
            {truncate(task.spec.summary, 110)}
          </p>
        </Link>
      </div>
      {/* The disclosure belongs to the first free cell because what it explains is why
          that task is first, and there is no answer to give for the second. */}
      {disclosure && <div className="text-xs text-dark-muted">{disclosure}</div>}
      {action && <div className="flex justify-end">{action}</div>}
    </div>
  );
}

function EmptyCell({ projectId, quiet }: { projectId: string; quiet: boolean }) {
  return (
    <div
      className={`${CELL_BASE} items-start border-dashed border-dark-border bg-dark-bg/40`}
    >
      <span className="text-xs uppercase tracking-wide text-dark-muted">Slot free</span>
      <p className="text-xs text-dark-muted">
        Nothing claimable to put in it — every open task is waiting on a dependency, or
        is an umbrella finished by its children.
      </p>
      {/* "Create task" is a call to action, so it goes quiet under an alarm for the
          same reason the Dispatch button does. */}
      {!quiet && (
        <Link
          to={projectPath(projectId, "/tasks/new")}
          className="touch-target text-xs text-blue-400 hover:text-blue-300"
        >
          Create task →
        </Link>
      )}
    </div>
  );
}

/**
 * Runway locks with no finish card to explain them, under the board.
 *
 * Until task-352 every finish was down here too, as "finishing task-092 · 4m" in a
 * footnote under three free cells, and the footnote was read as nothing happening. A
 * finish is now a card. What is left for this strip is a runway whose finish record the
 * server could not read -- rare, and still worth a line, because every other merge in
 * that repository is queued behind it.
 */
function RunwayStrip({ holders }: { holders: MachineHolderView[] }) {
  if (holders.length === 0) return null;
  return (
    <p data-testid="slot-board-holders" className="mt-3 text-xs text-dark-muted">
      <span className="font-medium text-dark-text">Also on this machine: </span>
      {holders
        .map((holder) => {
          const elapsed =
            holder.elapsed_seconds === null || holder.elapsed_seconds === undefined
              ? ""
              : ` · ${formatElapsed(holder.elapsed_seconds)}`;
          return `merge runway — ${holder.project_name || "a checkout"}${elapsed}`;
        })
        .join(", ")}
      . A lock, not a run slot — every other merge in that repository queues behind it.
    </p>
  );
}

export type SlotBoardProps = {
  /** The machine-wide answer. `null` while it is still being read. */
  body: LiveRunsView | null;
  /** This project's claimable frontier, in queue order, from the dashboard endpoint. */
  queue: TaskRead[];
  projectId: string;
  /**
   * An alarm is holding the page, so the board offers no action.
   *
   * It still draws every cell -- the machine's shape is worth knowing on a bad day too,
   * and a board that vanished whenever something needed Jeff would be a board he almost
   * never saw. What it withholds is the *nudge*: the Dispatch button on a free cell and
   * the closed-gate line explaining why there is none. task-081's rule is that an alarm
   * must never compete with a call to action, and a card that only links to a task is
   * not one -- the Active Tasks list below has been doing exactly that all along.
   */
  statusOnly?: boolean;
  /** The Dispatch control for one queued task, supplied by the page. */
  renderQueueAction?: (task: TaskRead) => React.ReactNode;
  /** Why the machine cannot dispatch at all, if it cannot. One line, at the foot. */
  renderQueueGate?: () => React.ReactNode;
  /** The "why this one" disclosure, rendered in the first free cell. */
  renderWhyThisOne?: () => React.ReactNode;
};

export function SlotBoard({
  body,
  queue,
  projectId,
  statusOnly = false,
  renderQueueAction,
  renderQueueGate,
  renderWhyThisOne,
}: SlotBoardProps) {
  // Nothing until the machine has answered. A board that painted a default number of
  // cells and then corrected itself one poll later would be a page whose shape is a
  // guess, which is the specific thing `dispatch_configured` exists to avoid claiming.
  if (!body) return null;

  const layout = boardLayout(body, queue, projectId);
  const runways = unexplainedRunways(body);
  if (layout.cells.length === 0 && runways.length === 0) return null;

  let firstFree = true;

  return (
    <section
      data-testid="slot-board"
      data-cells={layout.cells.length}
      data-status-only={statusOnly ? "true" : "false"}
      aria-label="Run slots"
      className="rounded-lg border border-dark-border bg-dark-surface p-3"
    >
      <div className="mb-2 flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <h2 className="text-sm font-medium text-dark-text">
          {layout.unconfigured ? "Next up" : "Run slots"}
        </h2>
        <div className="flex items-baseline gap-3">
          <span data-testid="slot-board-capacity" className="text-xs text-dark-muted">
            {capacitySentence(body)}
          </span>
          <Link
            to={projectPath(projectId, "/runs")}
            className="touch-target text-xs text-blue-400 hover:text-blue-300"
          >
            Running now →
          </Link>
        </div>
      </div>

      <div className={`grid gap-2 ${boardColumns(layout.cells.length)}`}>
        {layout.cells.map((cell) => {
          // `key` is what makes the ordering rule above visible to React: a cell keyed
          // on its run id survives a poll that reorders nothing, so the DOM node -- and
          // anything focused inside it -- is the same node it was two seconds ago.
          switch (cell.kind) {
            case "run":
              return (
                <div
                  key={cell.key}
                  data-testid="slot-cell"
                  data-slot-state="run"
                  data-run-id={cell.run.run_id}
                  data-task-id={cell.run.task_id}
                >
                  <RunCell run={cell.run} projectId={projectId} />
                </div>
              );
            case "opaque":
              return (
                <div key={cell.key} data-testid="slot-cell" data-slot-state="opaque">
                  <OpaqueCell />
                </div>
              );
            case "finish":
              return (
                <div
                  key={cell.key}
                  data-testid="slot-cell"
                  data-slot-state="finish"
                  data-finish-id={cell.finish.finish_id}
                  data-task-id={cell.finish.task_id}
                >
                  <FinishCell finish={cell.finish} projectId={projectId} />
                </div>
              );
            case "queued": {
              const disclosure = firstFree ? renderWhyThisOne : undefined;
              firstFree = false;
              return (
                <div
                  key={cell.key}
                  data-testid="slot-cell"
                  data-slot-state="queued"
                  data-task-id={cell.task.id}
                >
                  <QueuedCell
                    task={cell.task}
                    projectId={projectId}
                    label={layout.unconfigured ? "Next up" : "Slot free"}
                    action={statusOnly ? undefined : renderQueueAction?.(cell.task)}
                    disclosure={disclosure?.()}
                  />
                </div>
              );
            }
            case "empty":
              return (
                <div key={cell.key} data-testid="slot-cell" data-slot-state="empty">
                  <EmptyCell projectId={projectId} quiet={statusOnly} />
                </div>
              );
          }
        })}
      </div>

      {layout.hiddenSlots > 0 && (
        <p data-testid="slot-board-overflow" className="mt-3 text-xs text-dark-muted">
          {`+${layout.hiddenSlots} more ${layout.hiddenSlots === 1 ? "slot" : "slots"} this machine allows`}
          {layout.hiddenBusy > 0 ? `, ${layout.hiddenBusy} of them busy` : ""}
          {". The board draws "}
          {BOARD_CELL_LIMIT}
          {" so the page still fits a screen; "}
          <Link
            to={projectPath(projectId, "/runs")}
            className="text-blue-400 underline hover:text-blue-300"
          >
            Running now
          </Link>
          {" is the long form."}
        </p>
      )}
      {!statusOnly && renderQueueGate?.()}
      <RunwayStrip holders={runways} />
    </section>
  );
}
