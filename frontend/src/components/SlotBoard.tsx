import { Link } from "react-router-dom";

import type {
  ArmedProjectView,
  EpicWalkView,
  LiveRunView,
  LiveRunsView,
  MachineHolderView,
  QueuedDispatchView,
  StartPauseView,
  StatusCategory,
  TaskCardRead,
} from "../api/types";
import { formatElapsed } from "./DispatchPanel";
import { StatusChip, WALK_STATES } from "./StatusChip";
import {
  FinishBadge,
  HealthBadge,
  capacitySentence,
  finishDetail,
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
 * `runs` and not in `occupied`, because it holds its task rather than a slot. A **walk**
 * run, which handed an epic's children to the server and started no agent (task-458), is
 * added the same way and for the same reason; it is normally over before any board is
 * drawn, and one that is not must not be shown taking a cell it does not take.
 */
/**
 * An **overage** run -- one a person started above the ceiling (task-461) -- is a `run`
 * cell like any other, and the board simply has more of them than the ceiling. There is
 * no `overage` kind, because a cell's kind says what is in it rather than how it got
 * there, and the run itself carries `over_ceiling` for the badge.
 */
export type SlotCell =
  | { kind: "run"; key: string; run: LiveRunView }
  | { kind: "opaque"; key: string }
  | { kind: "finish"; key: string; finish: MachineHolderView }
  | { kind: "queued"; key: string; task: TaskCardRead }
  | { kind: "empty"; key: string };

export type BoardLayout = {
  cells: SlotCell[];
  /** Slots the ceiling claims that {@link BOARD_CELL_LIMIT} refused to draw. */
  hiddenSlots: number;
  /** How many of those are busy, so the footer can say the board is not the whole truth. */
  hiddenBusy: number;
  /** Runs above `max_concurrent_runs`, each a deliberate overage (task-461). */
  overCeiling: number;
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
/**
 * Whether this run is one of the machine's `occupied` slots.
 *
 * The server's own answer, read off the row rather than re-derived from `mode`. It used
 * to be a mirror of `LiveRun.takes_slot` written in TypeScript, and task-482 is what a
 * mirror costs: a third exemption arrived -- a run whose task has closed while its
 * session stayed open -- and a board deriving this from the mode would have kept drawing
 * that run in a slot cell the server had already freed. A run drawn in a cell the server
 * does not count is a board disagreeing with the dispatch guard about whether there is
 * room, which is the one thing this function exists to prevent.
 */
export function holdsSlot(run: LiveRunView): boolean {
  return run.holds_slot;
}

export function boardLayout(
  body: LiveRunsView,
  queue: TaskCardRead[],
  projectId: string,
): BoardLayout {
  const unconfigured = !body.dispatch_configured;
  // Slot cells are filled from the runs that *hold* slots. An interactive session is in
  // `runs` and not in `occupied` (task-354), so indexing the slot cells into the whole
  // list would put a chat session in a slot cell and push a dispatched run out of one.
  // The split is on holding a slot rather than on one mode's name, because task-458 made
  // a second mode that is in `runs` and not in `occupied`.
  const slotted = orderedRuns(body.runs.filter(holdsSlot));
  const attended = orderedRuns(body.runs.filter((run) => !holdsSlot(run)));
  // `occupied`, never `runs.length`. The count is the machine's and the list is this
  // caller's, and the cell that exists for the difference is `opaque`.
  // A machine may be *over* its ceiling: a person chose Dispatch now with every slot
  // taken (task-461). The board draws the extra runs rather than clipping them --
  // `Math.min(occupied, ceiling)` would have shown "1 of 1 busy" over one card while
  // two agents were writing to two repositories, which is the one thing a board of
  // slots must never do. Still bounded by BOARD_CELL_LIMIT, for the reason any large
  // ceiling is: past six cells this page stops fitting a viewport.
  const slots = unconfigured
    ? Math.min(body.occupied, BOARD_CELL_LIMIT)
    : Math.min(Math.max(body.max_concurrent_runs, body.occupied), BOARD_CELL_LIMIT);
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
    // How far past the ceiling this machine is running. Reported rather than left to
    // be inferred from the cell count, because the footer says it in words and the
    // board's cells are capped.
    overCeiling: unconfigured ? 0 : Math.max(0, body.occupied - body.max_concurrent_runs),
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
  // A run finishing itself is drawn as the finish it now is (task-533): the violet edge
  // a finish card has, and the step where the posture would go. It keeps its own lock
  // rather than taking a finish one, so this tile is the only cell its merge gets.
  const finishing = run.health === "finishing";
  return (
    <div
      data-run-id={run.run_id}
      className={`${CELL_BASE} ${finishing ? "border-violet-900/70" : "border-dark-border"} bg-dark-bg`}
    >
      <div className="min-w-0">
        <div className="mb-1 flex items-center justify-between gap-2">
          <HealthBadge health={run.health} />
          <span className="shrink-0 text-xs text-dark-muted">
            {formatElapsed(run.elapsed_seconds)}
          </span>
        </div>
        {/* The one cell that has to explain itself. Every other card on this board is a
            slot the machine allows; this one is a slot it does not, and a reader
            counting four cards against a ceiling of three needs the fourth to say so
            rather than be left to wonder which number is wrong (task-461). */}
        {run.over_ceiling && (
          <div
            data-testid="slot-over-ceiling"
            className="mb-1 inline-flex rounded bg-amber-900/50 px-1.5 py-0.5 text-[0.65rem] font-semibold uppercase tracking-wide text-amber-200"
          >
            Over the ceiling
          </div>
        )}
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
        <span className="truncate">
          {finishing
            ? finishDetail(run.finish_step, run.runway_behind)
            : elsewhere
              ? run.project_name
              : runKindLabel(run)}
        </span>
        {elsewhere && (
          <span className="shrink-0">{finishing ? run.project_name : runKindLabel(run)}</span>
        )}
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
        <span className="truncate">{finishDetail(finish.detail, finish.runway_behind)}</span>
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
  task: TaskCardRead;
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
            {truncate(task.summary, 110)}
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
 * The dispatches waiting for a slot, in the order they will start (task-459).
 *
 * **A rail under the cells rather than cells of its own**, and the distinction is the
 * one the board is built on: a cell is a slot, slots are what the machine has, and a
 * queued dispatch has no slot -- that is its entire situation. Drawing it as a cell
 * would say the machine is bigger than it is, which is the same mistake as counting a
 * finish against the ceiling.
 *
 * The order is the server's `position`, not this array's index. They agree today and
 * the server's is the one that is true: the rows are filtered by what this caller may
 * see, so numbering them here would print 1, 2, 3 over entries that are really 1, 2
 * and 5 and tell somebody their dispatch is next when it is not.
 */
/**
 * When the subscription reopens, in the zone of whoever is reading (task-463).
 *
 * **The zone is the whole point.** The server sends UTC because that is the only thing
 * it can be right about, and "paused until 23:40Z" is a sum a person has to do in their
 * head at the moment they least want to. `toLocaleTimeString` with no locale argument
 * uses the browser's own, which is the reader's.
 *
 * A reset in the past is still printed rather than hidden. It means the probe has not
 * run yet, which is a few seconds at most and is honest; replacing it with "any moment"
 * would be the board inventing a state the machine does not have.
 */
export function resetInLocalTime(iso: string | null | undefined): string {
  if (!iso) return "";
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) return "";
  return parsed.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

/** The one sentence a pause gets, wherever it is drawn. */
export function pauseSentence(pause: StartPauseView): string {
  const when = pause.resumes_by_itself
    ? `until ${resetInLocalTime(pause.resets_at)}`
    : "until the incident clears";
  return `Paused ${when}: ${pause.kind_word} on ${pause.runner}`;
}

/**
 * Why the machine is starting nothing, drawn above whichever rail it is holding.
 *
 * **It is status and never an action**, so it is drawn under an alarm and in
 * `statusOnly` like the rails themselves. The whole reason it exists is that a person
 * glancing at a board full of waiting work needs to know the machine is not broken and
 * does not need them -- which is the opposite of a call to action, and is exactly the
 * thing they would otherwise go and investigate.
 */
function PausedNotice({ pauses, scope }: { pauses: StartPauseView[]; scope: string }) {
  if (pauses.length === 0) return null;
  return (
    <ul
      data-testid={`slot-board-paused-${scope}`}
      data-paused={pauses.length}
      className="mb-2 space-y-1"
    >
      {pauses.map((pause) => (
        <li
          key={pause.incident_id}
          data-testid="start-pause"
          data-incident-id={pause.incident_id}
          data-kind={pause.kind}
          data-resets-at={pause.resets_at ?? ""}
          className="rounded-lg border border-dashed border-sky-800/70 bg-sky-950/30 px-3 py-2 text-xs text-sky-200"
        >
          <span className="font-medium">{pauseSentence(pause)}</span>
          <span className="ml-2 text-sky-300/70">
            nothing is wrong and nobody is needed — it starts again by itself
          </span>
        </li>
      ))}
    </ul>
  );
}

/** The pauses holding at least one of these queued entries, newest incident last. */
export function pausesHoldingQueue(
  queued: QueuedDispatchView[],
  pauses: StartPauseView[],
): StartPauseView[] {
  const held = new Set(queued.map((entry) => entry.paused_by).filter(Boolean));
  return pauses.filter((pause) => held.has(pause.incident_id));
}

/** The pauses holding at least one of these armed projects. */
export function pausesHoldingArmings(
  armed: ArmedProjectView[],
  pauses: StartPauseView[],
): StartPauseView[] {
  const held = new Set(armed.map((entry) => entry.paused_by).filter(Boolean));
  return pauses.filter((pause) => held.has(pause.incident_id));
}

function WaitingRail({
  queued,
  projectId,
  limit,
  pauses,
  renderAction,
}: {
  queued: QueuedDispatchView[];
  projectId: string;
  limit: number;
  pauses: StartPauseView[];
  renderAction?: (entry: QueuedDispatchView) => React.ReactNode;
}) {
  if (queued.length === 0) return null;
  return (
    <section
      data-testid="slot-board-queue"
      data-queued={queued.length}
      aria-label="Dispatches waiting for a slot"
      className="mt-3 border-t border-dark-border pt-3"
    >
      <div className="mb-2 flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        {/* "Queued", the word a task in this state carries on its own chip (task-562).
            The sentence is the owner's, and true of the controller today: a free slot
            goes to the queue before a walk's next child (task-477). */}
        <h3 className="text-xs font-medium uppercase tracking-wide text-dark-muted">Queued</h3>
        <span className="text-xs text-dark-muted">
          {queued.length} of {limit || queued.length} · Queued tasks are taken before epic
          walk tasks.
        </span>
      </div>
      <PausedNotice pauses={pauses} scope="queue" />
      <ol className="space-y-2">
        {queued.map((entry) => (
          <li
            key={entry.queue_id}
            data-testid="queued-dispatch"
            data-queue-id={entry.queue_id}
            data-task-id={entry.task_id}
            data-position={entry.position}
            data-paused-by={entry.paused_by || undefined}
            className="flex items-center justify-between gap-3 rounded-lg border border-dashed border-amber-900/70 bg-dark-bg p-3"
          >
            <div className="flex min-w-0 items-center gap-3">
              <span className="shrink-0 rounded bg-amber-900/40 px-2 py-1 font-mono text-xs text-amber-200">
                {entry.position}
              </span>
              <Link
                to={entry.task_url || projectPath(projectId, `/tasks/${entry.task_id}`)}
                className="block min-w-0 hover:text-blue-300"
              >
                <div className="truncate font-mono text-xs text-blue-400">{entry.task_id}</div>
                <div className="line-clamp-2 text-sm font-medium text-dark-text">
                  {entry.task_title || entry.queue_id}
                </div>
              </Link>
            </div>
            <div className="flex shrink-0 items-center gap-3 text-xs text-dark-muted">
              <span className="text-right">
                {/* What a person wants here is how long it has been sitting, and whether
                    the queue is actually moving. `starting` is the few seconds a tick is
                    putting it through the gates, and saying so is what stops a card that
                    changes under the cursor looking like a glitch. */}
                {/* A paused entry is not "waiting for a slot" even when it is also
                    doing that: the slot may well be free. Saying so on the row keeps
                    the notice above from having to be read as applying to all of them
                    on a machine where only some are held. */}
                {entry.paused_by
                  ? `paused · ${waitedFor(entry)}`
                  : entry.status === "starting"
                    ? "starting…"
                    : waitedFor(entry)}
                {entry.queued_by ? ` · ${entry.queued_by}` : ""}
              </span>
              {renderAction?.(entry)}
            </div>
          </li>
        ))}
      </ol>
      {/* Only where a start has already been tried and put back. Silence is the normal
          case and means the only thing in the way is the ceiling. */}
      {queued.some((entry) => entry.detail) && (
        <p data-testid="slot-board-queue-detail" className="mt-2 text-xs text-dark-muted">
          {queued.find((entry) => entry.detail)?.detail}
        </p>
      )}
    </section>
  );
}

/**
 * The projects the pull mode is armed for, and what each would start next (task-462).
 *
 * **A rail, like the waiting dispatches, and for the opposite reason.** A queued
 * dispatch has no slot; an arming has no slot *yet* and will take whichever one frees.
 * Neither is a cell, because a cell is a slot the machine actually has, and drawing
 * either as one would say the machine is bigger than it is.
 *
 * **It names what starts next.** That is the affordance the mode needs and the reason
 * the server sends it: the whole steering mechanism is the stored queue order, so a
 * person who disagrees with the choice has to be able to see it *before* it happens and
 * go and move something. A board that only said "armed" would leave them finding out
 * from a run that had already started.
 *
 * Drawn even under an alarm, like the waiting rail: a machine that will keep starting
 * work on its own is status, and arguably the most important status on the page. The one
 * thing `statusOnly` withholds is Disarm, which is an action.
 */
/**
 * How one walk's progress reads, as the counts a person would act on.
 *
 * Exported so the numbers can be asserted as the sentence a reader sees rather than as
 * three attributes on a container: the failure this section exists to prevent is
 * somebody misreading the board, and a test that checks an attribute passes over every
 * way of rendering that number wrongly.
 *
 * An unreadable graph says so instead of printing zeroes. A project unregistered while
 * its walk is still open is exactly the row worth keeping visible, and "0 of 0 children
 * done" there would read as a finished epic.
 */
export function walkCountsSentence(walk: EpicWalkView): string {
  if (walk.children_total === 0) {
    return "children could not be read from this project's backlog";
  }
  return [
    `${walk.children_completed} of ${walk.children_total} children done`,
    `${walk.children_in_flight} in flight`,
    `${walk.children_remaining} to come`,
  ].join(" · ");
}

/**
 * The badge and the sentence for what a walk is doing, which is one decision.
 *
 * **Grounded, waiting and walking are three states and the middle one is the trap.** A
 * grounded walk has stopped taking off; a waiting one has grounded on something that
 * clears by itself and resumes with nobody involved (task-467). Rendering the two the
 * same way makes a reader either chase a walk that needs nothing or ignore one that has
 * stopped for good, and the board is the only place either mistake gets made.
 */
export function walkState(walk: EpicWalkView): {
  badge: string;
  category: StatusCategory;
  sentence: string;
} {
  // The words and categories are the status data file's (task-562), because the owner
  // reads these on the same board as the task chips: walking is running (blue), waiting
  // clears by itself (pink), and grounded will not continue until a person acts (red).
  if (!walk.grounded) {
    return {
      badge: WALK_STATES.walking.label,
      category: WALK_STATES.walking.category,
      sentence: "starting each child as its dependencies close",
    };
  }
  const because = walk.grounded_word || walk.grounded_reason;
  if (walk.resumes_by_itself) {
    const on = walk.waiting_on_task_id ? ` on ${walk.waiting_on_task_id}` : "";
    return {
      badge: WALK_STATES.waiting.label,
      category: WALK_STATES.waiting.category,
      sentence: `waiting${on}: ${because}. It takes off again on its own when that clears.`,
    };
  }
  return {
    badge: WALK_STATES.grounded.label,
    category: WALK_STATES.grounded.category,
    sentence: `grounded: ${because}. Nothing more takes off until a person acts.`,
  };
}

/**
 * The epics this machine is supervising, under the waiting rail and above the armed one.
 *
 * **A walk is the one thing here that dispatches with no human act at the moment of
 * dispatch**, and since task-458 it is hosted by the server rather than by a blocking
 * process -- so its own run is normally over before this board is drawn and the page
 * showed only the consequences: a slot filling, a task going agent/work, and nothing
 * saying why (task-523).
 *
 * It holds no slot and is drawn below the grid for that reason, in the same shape as
 * the waiting and armed rails: the cells above it, their count, and what the free ones
 * offer are exactly what they would be without it.
 *
 * Nothing at all when no walk is open. A header reading "Epic walks (0)" on every calm
 * day is the page's loudest element saying nothing, which is the argument that keeps
 * the board itself off an empty project.
 */
function WalkRail({ walks }: { walks: EpicWalkView[] }) {
  if (walks.length === 0) return null;
  return (
    <section
      data-testid="slot-board-walks"
      data-walks={walks.length}
      aria-label="Epics being walked"
      className="mt-3 border-t border-dark-border pt-3"
    >
      <div className="mb-2 flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <h3 className="text-xs font-medium uppercase tracking-wide text-dark-muted">
          Epics being walked
        </h3>
        <span className="text-xs text-dark-muted">
          children start themselves from here, and it holds no slot of its own
        </span>
      </div>
      <ol className="space-y-2">
        {walks.map((walk) => {
          const state = walkState(walk);
          return (
            <li
              key={walk.walk_id}
              data-testid="epic-walk"
              data-walk-id={walk.walk_id}
              data-grounded={walk.grounded ? "true" : "false"}
              // Neutral, not violet: purple is Finishing's and nothing else's (task-562).
              className="rounded-lg border border-dashed border-dark-border bg-dark-bg p-3"
            >
              <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
                <Link
                  to={
                    walk.parent_task_url ||
                    projectPath(walk.project_id, `/tasks/${walk.parent_task_id}`)
                  }
                  data-testid="epic-walk-parent"
                  className="min-w-0 hover:text-blue-300"
                >
                  <span className="font-mono text-xs text-blue-400">{walk.parent_task_id}</span>
                  <span className="ml-2 text-sm text-dark-text">{walk.parent_task_title}</span>
                </Link>
                <StatusChip
                  testId="epic-walk-badge"
                  category={state.category}
                  label={state.badge}
                />
              </div>
              <p data-testid="epic-walk-counts" className="mt-1 text-xs text-dark-muted">
                {walkCountsSentence(walk)}
              </p>
              <p data-testid="epic-walk-state" className="mt-1 text-xs text-dark-muted">
                {state.sentence}
              </p>
              {walk.waiting_on_task_id && (
                <Link
                  to={
                    walk.waiting_on_task_url ||
                    projectPath(walk.project_id, `/tasks/${walk.waiting_on_task_id}`)
                  }
                  data-testid="epic-walk-waiting-on"
                  className="mt-1 block min-w-0 hover:text-blue-300"
                >
                  <span className="font-mono text-xs text-blue-400">
                    {walk.waiting_on_task_id}
                  </span>
                  <span className="ml-2 text-xs text-dark-text">{walk.waiting_on_task_title}</span>
                </Link>
              )}
            </li>
          );
        })}
      </ol>
    </section>
  );
}

function ArmedRail({
  armed,
  pauses,
  renderAction,
}: {
  armed: ArmedProjectView[];
  pauses: StartPauseView[];
  renderAction?: (entry: ArmedProjectView) => React.ReactNode;
}) {
  if (armed.length === 0) return null;
  return (
    <section
      data-testid="slot-board-armed"
      data-armed={armed.length}
      aria-label="Projects the pull mode is armed for"
      className="mt-3 border-t border-dark-border pt-3"
    >
      <div className="mb-2 flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <h3 className="text-xs font-medium uppercase tracking-wide text-dark-muted">
          Pulling from the queue
        </h3>
        <span className="text-xs text-dark-muted">
          free slots fill themselves, with every dispatch gate checked at the start
        </span>
      </div>
      <PausedNotice pauses={pauses} scope="armed" />
      <ol className="space-y-2">
        {armed.map((entry) => (
          <li
            key={entry.arming_id}
            data-testid="armed-project"
            data-arming-id={entry.arming_id}
            data-project-id={entry.project_id}
            data-paused-by={entry.paused_by || undefined}
            className="flex items-center justify-between gap-3 rounded-lg border border-dashed border-orange-800/70 bg-dark-bg p-3"
          >
            <div className="min-w-0">
              <div className="text-sm font-medium text-dark-text">
                {entry.project_name || entry.project_id}
                <span className="ml-2 text-xs text-dark-muted" data-testid="armed-bound">
                  {entry.armed_by ? `armed by ${entry.armed_by} · ` : ""}
                  {entry.bound}
                  {entry.posture ? ` · ${entry.posture}` : ""}
                </span>
                {/* On the row as well as in the notice, because the bound beside it is
                    the thing a person would otherwise assume was being spent. It is
                    not: a paused arming starts nothing and keeps every start it has. */}
                {entry.paused_by && (
                  <span
                    data-testid="armed-paused"
                    className="ml-2 rounded bg-sky-900/40 px-2 py-0.5 text-xs text-sky-200"
                  >
                    paused · bound untouched
                  </span>
                )}
              </div>
              {entry.next_task_id ? (
                <Link
                  to={entry.next_task_url || projectPath(entry.project_id, `/tasks/${entry.next_task_id}`)}
                  data-testid="armed-next"
                  className="mt-1 block min-w-0 hover:text-blue-300"
                >
                  <span className="text-xs text-dark-muted">next: </span>
                  <span className="font-mono text-xs text-blue-400">{entry.next_task_id}</span>
                  <span className="ml-2 text-sm text-dark-text">{entry.next_task_title}</span>
                </Link>
              ) : (
                <p className="mt-1 text-xs text-dark-muted" data-testid="armed-next-empty">
                  next: nothing claimable in that queue right now
                </p>
              )}
            </div>
            <div className="flex shrink-0 items-center gap-3 text-xs text-dark-muted">
              {renderAction?.(entry)}
            </div>
          </li>
        ))}
      </ol>
      {/* The precedence rule, said where it is visible rather than only in the design
          doc: somebody watching a queued dispatch and an arming compete for one slot
          should be able to read which wins off the board. */}
      <p className="mt-2 text-xs text-dark-muted">
        A dispatch you asked for by name starts before any of these.
      </p>
    </section>
  );
}

/** Whether this project is one of the armed ones, for the board's own header. */
export function armedHere(armed: ArmedProjectView[], projectId: string): boolean {
  return armed.some((entry) => entry.project_id === projectId);
}

/** How long one entry has been waiting, or the moment it joined when that is unknown. */
export function waitedFor(entry: QueuedDispatchView): string {
  if (entry.waiting_seconds === null || entry.waiting_seconds === undefined) {
    return "queued";
  }
  return `waiting ${formatElapsed(entry.waiting_seconds)}`;
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
  queue: TaskCardRead[];
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
  renderQueueAction?: (task: TaskCardRead) => React.ReactNode;
  /** Why the machine cannot dispatch at all, if it cannot. One line, at the foot. */
  renderQueueGate?: () => React.ReactNode;
  /** The "why this one" disclosure, rendered in the first free cell. */
  renderWhyThisOne?: () => React.ReactNode;
  /**
   * The control for one waiting dispatch, supplied by the page (task-459).
   *
   * Cancel, in practice. It is the page's to render for the reason the Dispatch button
   * is: this component knows the machine's shape and nothing about mutating it, and a
   * cancel wired in here would have to carry a project, a mutation and a busy state
   * that belong to whoever is already holding them.
   */
  renderQueuedAction?: (entry: QueuedDispatchView) => React.ReactNode;
  /**
   * The control for one armed project, supplied by the page (task-462).
   *
   * Disarm, in practice, and the page's for the same reason Cancel is: this component
   * knows the machine's shape and nothing about mutating it.
   */
  renderArmedAction?: (entry: ArmedProjectView) => React.ReactNode;
};

export function SlotBoard({
  body,
  queue,
  projectId,
  statusOnly = false,
  renderQueueAction,
  renderQueueGate,
  renderWhyThisOne,
  renderQueuedAction,
  renderArmedAction,
}: SlotBoardProps) {
  // Nothing until the machine has answered. A board that painted a default number of
  // cells and then corrected itself one poll later would be a page whose shape is a
  // guess, which is the specific thing `dispatch_configured` exists to avoid claiming.
  if (!body) return null;

  const layout = boardLayout(body, queue, projectId);
  const runways = unexplainedRunways(body);
  const waiting = body.queued ?? [];
  const armed = body.armed ?? [];
  const walks = body.walks ?? [];
  const pauses = body.paused ?? [];
  if (
    layout.cells.length === 0 &&
    runways.length === 0 &&
    waiting.length === 0 &&
    armed.length === 0 &&
    walks.length === 0
  )
    return null;

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

      {layout.overCeiling > 0 && (
        <p data-testid="slot-board-over-ceiling" className="mt-3 text-xs text-amber-300">
          {layout.overCeiling === 1
            ? "1 run above this machine's ceiling, started by a person who chose to. "
            : `${layout.overCeiling} runs above this machine's ceiling, each started by a person who chose to. `}
          {"The count above is the truth rather than a number clipped to the limit, and "}
          {"the machine stays over it until they end."}
        </p>
      )}

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
      {/* Above the gate line and the runway strip, and drawn even under an alarm:
          work this machine has already been told to do is status, not a nudge. The one
          thing `statusOnly` withholds is the cancel control, which is an action. */}
      <WaitingRail
        queued={waiting}
        projectId={projectId}
        limit={body.queue_limit ?? 0}
        pauses={pausesHoldingQueue(waiting, pauses)}
        renderAction={statusOnly ? undefined : renderQueuedAction}
      />
      {/* Second, under the queued rail: the rails are drawn in the order they get the
          next free slot, and the queue is served before walk children (the owner's
          decision on task-477; docs/agent-dispatch-design.md section 7). Task-523 had
          put this directly under the cells, because a walk explains children appearing
          in the slots above; task-557 moved it here, accepting one rail between the
          slots and that explanation. It takes nothing from `statusOnly` because it
          offers no action to withhold -- task-523 is read-only about walks on purpose. */}
      <WalkRail walks={walks} />
      {/* Under the walk rail, which is the order they are honoured in: a dispatch
          somebody asked for by name, and then a walk's next child, start before the
          pull mode fills anything. */}
      <ArmedRail
        armed={armed}
        pauses={pausesHoldingArmings(armed, pauses)}
        renderAction={statusOnly ? undefined : renderArmedAction}
      />
      {!statusOnly && renderQueueGate?.()}
      <RunwayStrip holders={runways} />
    </section>
  );
}
