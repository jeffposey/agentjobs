import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import type {
  BrokenTaskFile,
  QueueMovePlacement,
  QueueMoveWarning,
  QueueProblemRead,
  TaskRead,
} from "../api/types";
import { BrokenFiles } from "./BrokenFiles";
import { DependencyState } from "./DependencyState";
import { startDragAutoScroll } from "./dragAutoScroll";
import { QueueBroken } from "./QueueBroken";
import { ResponsiveCell, ResponsiveTable, ResponsiveTableRow } from "./ResponsiveTable";
import {
  applyMove,
  bandMembers,
  bandOf,
  describeMove,
  isInQueue,
  stepMove,
  type QueueMove,
  type StepDirection,
} from "./queueOrder";

type TaskRow = {
  task: TaskRead;
  depth: number;
  ancestors: Array<string>;
  childCount: number;
  openChildren: number;
};

/** What the queue-move check said about a move that has already landed. */
export type MoveVerdict = {
  warnings: Array<QueueMoveWarning>;
  undo: QueueMovePlacement | null;
};

/** The three server verbs a person can reach from this list, and nothing else. */
export type ReorderHandlers = {
  move: (taskId: string, move: QueueMove) => Promise<MoveVerdict>;
  reprioritize: (taskId: string, priority: string, before: string) => Promise<void>;
  keep: (taskId: string) => Promise<void>;
};

/**
 * A placement, as the move handler takes one.
 *
 * The server answers `queue_undo` in its own shape -- kind and target -- because that
 * is what the record stores; this is the one place that translates. Null for a
 * placement naming a neighbour it did not send, which is not a shape the server
 * produces and is refused here rather than sent as a move to nowhere.
 */
export function undoMove(placement: QueueMovePlacement | null): QueueMove | null {
  if (!placement) return null;
  if (placement.kind === "top") return { top: true };
  if (placement.kind === "bottom") return { bottom: true };
  if (!placement.target) return null;
  return placement.kind === "before" ? { before: placement.target } : { after: placement.target };
}

const STATUS_FILTERS = new Set(["all", "open", "draft", "ready", "active", "human", "external", "closed"]);
const PRIORITY_FILTERS = new Set(["all", "critical", "high", "medium", "low"]);
const SCOPE_FILTERS = new Set(["all", "project", "test"]);
const PRIORITY_CLASSES: Record<string, string> = {
  critical: "bg-red-900 text-red-200",
  high: "bg-orange-900 text-orange-200",
  medium: "bg-yellow-900 text-yellow-200",
  low: "bg-slate-700 text-slate-200",
};
/**
 * The drag payload's MIME type. Private on purpose -- see the `onDragStart` comment.
 */
const DRAG_TYPE = "application/x-agentjobs-task-id";
const STEP_KEYS: Record<string, StepDirection> = {
  ArrowUp: "up",
  ArrowDown: "down",
  Home: "top",
  End: "bottom",
};

/**
 * Rows in the order the server sent them, grouped under their parents.
 *
 * **There is no sort here, deliberately.** Until task-207 this sorted by `updated`
 * descending and consulted priority only to break a tie, so the list a human read was
 * ordered by one rule while the scheduler answered by another — and neither rule had
 * been chosen by anybody. `manager.list_tasks` now settles the order in
 * `(band, queue_position)`, and the client's job is to not undo that.
 *
 * The parent grouping is a regrouping, not a re-sort: siblings keep the order they
 * arrived in, so within any one group the queue's order survives. It does move a child
 * away from its own band's run of rows, which is why the position column exists and
 * why every reorder gesture is computed over the band rather than over the rows on
 * screen. The default filters flatten the list anyway — grouping only appears when the
 * status filter is `all` and nothing else is set.
 */
export function buildTaskRows(tasks: Array<TaskRead>): Array<TaskRow> {
  const byId = new Map(tasks.map((task) => [task.id, task]));
  const children = new Map<string | null, Array<TaskRead>>();

  for (const task of tasks) {
    const parent = task.parent && byId.has(task.parent) ? task.parent : null;
    children.set(parent, [...(children.get(parent) ?? []), task]);
  }

  const rows: Array<TaskRow> = [];
  const drawn = new Set<string>();
  const walk = (task: TaskRead, ancestors: Array<string>) => {
    if (drawn.has(task.id)) return;
    const kids = children.get(task.id) ?? [];
    rows.push({
      task,
      depth: ancestors.length,
      ancestors,
      childCount: kids.length,
      openChildren: kids.filter((child) => child.lifecycle !== "closed").length,
    });
    drawn.add(task.id);
    for (const child of kids) {
      if (child.id === task.id || ancestors.includes(child.id)) continue;
      walk(child, [...ancestors, task.id]);
    }
  };

  for (const root of children.get(null) ?? []) walk(root, []);

  // Cycles have no root. Append every undrawn record flat so malformed ancestry can
  // never make a task disappear from the list.
  for (const task of tasks) {
    if (!drawn.has(task.id)) {
      rows.push({ task, depth: 0, ancestors: [], childCount: 0, openChildren: 0 });
    }
  }
  return rows;
}

function matchesTask(task: TaskRead, search: string, status: string, priority: string, scope: string) {
  const term = search.trim().toLowerCase();
  // The id is searched as well as the title because the id is what people quote:
  // "058" and "task-058" both have to find task-058-multi-project-gui. Summary and
  // description are deliberately left out -- this box filters a visible list, and a
  // row matching on text the row does not show reads as a bug. The API's /api/search
  // is the full-text surface.
  const titleMatches = term === ""
    || task.title.toLowerCase().includes(term)
    || task.id.toLowerCase().includes(term);
  const statusMatches = status === "all"
    || (status === "open" && task.lifecycle !== "closed")
    || task.lifecycle === status
    || task.ball === status;
  const priorityMatches = priority === "all" || task.priority === priority;
  const tags = task.tags ?? [];
  const isTest = tags.includes("test") || tags.includes("example");
  const scopeMatches = scope === "all" || (scope === "test" ? isTest : !isTest);
  return titleMatches && statusMatches && priorityMatches && scopeMatches;
}

function filterValue(params: URLSearchParams, key: string, allowed: Set<string>, fallback: string) {
  const candidate = params.get(key) ?? fallback;
  return allowed.has(candidate) ? candidate : fallback;
}

/**
 * The paragraph above the table describing the reorder keys.
 *
 * Every handle points at it with `aria-describedby` rather than carrying the
 * instructions in its own name. It is rendered exactly when reordering is available,
 * which is also exactly when a handle exists, so the reference never dangles.
 */
const REORDER_HELP_ID = "queue-reorder-help";

/** The reorder handle's own id, so focus can be put back on it after a step. */
function gripId(taskId: string) {
  return `queue-grip-${taskId}`;
}

function taskPath(projectId: string, taskId: string) {
  return `/p/${encodeURIComponent(projectId)}/tasks/${encodeURIComponent(taskId)}`;
}

/** A fingerprint of the order the server last sent, used to expire a prediction. */
function orderSignature(tasks: Array<TaskRead>) {
  return tasks.map((task) => `${task.id}:${task.queue_position ?? ""}`).join("|");
}

type PendingMove = { signature: string; tasks: Array<TaskRead> };
type MoveNotice = { taskId: string } & MoveVerdict;
type BandChange = { taskId: string; from: string; to: string; before: string };

export function TaskList({
  tasks,
  brokenFiles,
  projectId,
  queueProblems = [],
  repairCommand = "agentjobs queue repair",
  reorder = null,
  reorderUnavailable = null,
}: {
  tasks: Array<TaskRead>;
  brokenFiles: Array<BrokenTaskFile>;
  projectId: string;
  queueProblems?: Array<QueueProblemRead>;
  repairCommand?: string;
  reorder?: ReorderHandlers | null;
  reorderUnavailable?: string | null;
}) {
  const [params, setParams] = useSearchParams();
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());
  // An optimistic reorder, kept beside a fingerprint of the data it was predicted
  // from. When the server's answer arrives the fingerprint no longer matches and the
  // prediction is dropped -- no effect, no timer, no second render pass -- so the
  // screen cannot keep showing a guess after the truth has landed.
  const [pending, setPending] = useState<PendingMove | null>(null);
  const [announcement, setAnnouncement] = useState("");
  const [moveError, setMoveError] = useState<string | null>(null);
  // What the queue-move check said about the last move, if it said anything. Held in
  // state rather than derived, because it is a fact about an event: the task refetches
  // a moment later and carries no trace of what the person who moved it was told.
  const [notice, setNotice] = useState<MoveNotice | null>(null);
  const [keeping, setKeeping] = useState(false);
  // Which move the notice on screen is allowed to be about.
  //
  // Two Alt+Up presses is one gesture as far as a person is concerned, and each fires
  // its own request and its own refetch. Those do not have to finish in the order they
  // started -- and they did not, in a browser, on the first fixture this was tried
  // against: the second move's findings appeared and were then overwritten by the
  // first move's, leaving a notice describing a state the queue was no longer in.
  // Every verdict therefore carries the move it belongs to, and a late one is dropped.
  const moveCount = useRef(0);
  const [bandChange, setBandChange] = useState<BandChange | null>(null);
  const [dragging, setDragging] = useState<string | null>(null);
  // The task whose handle should hold focus after the next render.
  const restoreFocus = useRef<string | null>(null);

  const search = params.get("q") ?? "";
  const status = filterValue(params, "status", STATUS_FILTERS, "open");
  const priority = filterValue(params, "priority", PRIORITY_FILTERS, "all");
  const scope = filterValue(params, "scope", SCOPE_FILTERS, "all");
  const flattened = search.trim() !== "" || status !== "all" || priority !== "all" || scope !== "all";

  const signature = useMemo(() => orderSignature(tasks), [tasks]);
  const ordered = pending && pending.signature === signature ? pending.tasks : tasks;
  const rows = useMemo(() => buildTaskRows(ordered), [ordered]);
  const visibleRows = rows.filter((row) => {
    if (!matchesTask(row.task, search, status, priority, scope)) return false;
    return flattened || row.ancestors.every((ancestor) => expanded.has(ancestor));
  });

  // A band with two tasks on one number is not an order, so the list stops offering to
  // change *that* band: every gesture places a task relative to a neighbour, and a
  // neighbour's position is exactly what corruption makes untrustworthy.
  //
  // Scoped per band rather than corpus-wide, following the same reasoning selection
  // uses (design section 8): a duplicate in `low` does not falsify anything about the
  // `high` order, and taking the whole screen's reordering away over it would punish
  // the wrong band. The banner is corpus-wide because seeing the damage is the point.
  const brokenBands = useMemo(
    () => new Set(queueProblems.map((problem) => problem.band)),
    [queueProblems],
  );
  const handlers = reorder;
  // Nothing is said twice: where the queue is broken the banner above has already said
  // it at more length than a footnote could.
  const unavailableReason = brokenBands.size > 0 ? null : reorderUnavailable;

  // Scroll the page while a drag is held near the top or bottom of the window.
  //
  // Keyed on `dragging`, so the loop exists only for a drag this list started: a link
  // or a file dragged in from outside never moves the page. `startDragAutoScroll` also
  // tears itself down on drop and dragend, so the loop cannot outlive the gesture even
  // if this state were somehow left set.
  useEffect(() => {
    if (!dragging) return;
    return startDragAutoScroll();
  }, [dragging]);

  // Put focus back on the handle of the task that just moved.
  //
  // Without this the keyboard path works exactly once. React reorders the rows by
  // moving their DOM nodes, and a browser drops focus from a node that is detached and
  // reinserted -- so the second Alt+Down of a two-step reorder either does nothing or,
  // worse, moves whichever task slid into the vacated row. Neither a jsdom test nor a
  // Playwright test that focuses the handle before every press can see this; it was
  // found by pressing the key twice in a real browser.
  useLayoutEffect(() => {
    const taskId = restoreFocus.current;
    if (!taskId) return;
    restoreFocus.current = null;
    document.getElementById(gripId(taskId))?.focus();
  });

  const updateParam = (key: string, value: string, fallback: string) => {
    const next = new URLSearchParams(params);
    if (value === fallback) next.delete(key);
    else next.set(key, value);
    setParams(next, { replace: true });
  };

  const toggle = (id: string) => {
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const runMove = async (taskId: string, move: QueueMove | null) => {
    if (!handlers || !move) return;
    setMoveError(null);
    setNotice(null);
    const sequence = (moveCount.current += 1);
    restoreFocus.current = taskId;
    setPending({ signature, tasks: applyMove(ordered, taskId, move) });
    setAnnouncement(describeMove(ordered, taskId, move));
    try {
      // The check runs on every move, including an undo. One path rather than a
      // special case: an undo is an ordinary move and anything the check says about
      // it is true of the queue as it now stands, so suppressing it here would mean
      // the one move whose consequences nobody was shown.
      const verdict = await handlers.move(taskId, move);
      if (sequence !== moveCount.current) return;
      if (verdict.warnings.length > 0) setNotice({ taskId, ...verdict });
    } catch {
      if (sequence !== moveCount.current) return;
      // Put back the way the server has it, rather than left showing a place the task
      // is not in. A screen that quietly disagrees with the record is worse than a
      // gesture that failed loudly, because the next decision is made from the screen.
      setPending(null);
      setAnnouncement("");
      setNotice(null);
      setMoveError(
        `${taskId} could not be moved, so the list has been put back the way the server has it. Reload and try again.`,
      );
    }
  };

  const keepMove = async () => {
    if (!handlers || !notice || keeping) return;
    const taskId = notice.taskId;
    setKeeping(true);
    try {
      await handlers.keep(taskId);
      setNotice(null);
      setAnnouncement(
        `${taskId} kept where it is. Its place is now anchored against automatic reordering.`,
      );
    } catch {
      setMoveError(
        `${taskId} could not be anchored, so nothing was recorded. The move itself still stands.`,
      );
    } finally {
      setKeeping(false);
    }
  };

  const confirmBandChange = async () => {
    if (!handlers || !bandChange) return;
    const change = bandChange;
    setBandChange(null);
    setMoveError(null);
    // No optimistic update for this one. A band change moves the task into an ordering
    // this screen holds no numbers for: unlike a step inside a band, there is no
    // neighbouring position to deal back out, so any guess would be a number invented
    // in the browser. The refetch is the answer.
    try {
      await handlers.reprioritize(change.taskId, change.to, change.before);
      setAnnouncement(
        `${change.taskId} moved from the ${change.from} band to the ${change.to} band, ahead of ${change.before}.`,
      );
    } catch {
      setMoveError(
        `${change.taskId} could not be reprioritised, so nothing was changed. Reload and try again.`,
      );
    }
  };

  /** Whether this row's place in line is a thing anybody may change right now. */
  const movableRow = (task: TaskRead) =>
    Boolean(handlers) && isInQueue(task) && !brokenBands.has(bandOf(task));

  const onRowKeyDown = (event: React.KeyboardEvent<HTMLTableRowElement>, task: TaskRead) => {
    const direction = STEP_KEYS[event.key];
    if (!event.altKey || !direction || !movableRow(task)) return;
    // Alt+Home and Alt+End would otherwise scroll the page away from the row that just
    // moved, and Alt+Arrow is back/forward in some browsers.
    event.preventDefault();
    void runMove(task.id, stepMove(ordered, task.id, direction));
  };

  const onRowDrop = (task: TaskRead) => {
    const sourceId = dragging;
    setDragging(null);
    if (!handlers || !sourceId || sourceId === task.id || !movableRow(task)) return;
    const source = ordered.find((candidate) => candidate.id === sourceId);
    if (!source || !movableRow(source)) return;
    if (bandOf(source) !== bandOf(task)) {
      // Two decisions in one gesture -- where it stands, and how urgent it is. The
      // second is asked out loud rather than inferred from where a finger let go.
      setBandChange({ taskId: source.id, from: bandOf(source), to: bandOf(task), before: task.id });
      return;
    }
    const band = bandMembers(ordered, bandOf(source));
    const from = band.findIndex((candidate) => candidate.id === source.id);
    const to = band.findIndex((candidate) => candidate.id === task.id);
    void runMove(source.id, from < to ? { after: task.id } : { before: task.id });
  };

  return (
    <div className="space-y-6">
      <BrokenFiles files={brokenFiles} />
      <QueueBroken problems={queueProblems} repairCommand={repairCommand} />
      <section className="rounded-lg border border-dark-border bg-dark-surface p-4" aria-label="Task filters">
        <div className="grid gap-3 min-[820px]:grid-cols-[minmax(16rem,1fr)_repeat(3,minmax(9rem,auto))]">
          <label className="sr-only" htmlFor="task-search">Search tasks</label>
          <input
            id="task-search"
            type="search"
            value={search}
            onChange={(event) => updateParam("q", event.target.value, "")}
            placeholder="Search title or id (e.g. 058)"
            className="touch-target w-full rounded-lg border border-dark-border bg-dark-bg px-4 text-dark-text focus:border-blue-500 focus:outline-none"
          />
          <label className="sr-only" htmlFor="status-filter">Status</label>
          <select id="status-filter" aria-label="Status" value={status} onChange={(event) => updateParam("status", event.target.value, "open")} className="touch-target w-full rounded-lg border border-dark-border bg-dark-bg px-3">
            <option value="open">Open (not closed)</option><option value="all">All Status</option><option value="draft">Draft</option><option value="ready">Ready</option><option value="active">Active</option><option value="human">Needs Human</option><option value="external">Blocked</option><option value="closed">Closed</option>
          </select>
          <label className="sr-only" htmlFor="priority-filter">Priority</label>
          <select id="priority-filter" aria-label="Priority" value={priority} onChange={(event) => updateParam("priority", event.target.value, "all")} className="touch-target w-full rounded-lg border border-dark-border bg-dark-bg px-3">
            <option value="all">All Priorities</option><option value="critical">Critical</option><option value="high">High</option><option value="medium">Medium</option><option value="low">Low</option>
          </select>
          <label className="sr-only" htmlFor="scope-filter">Scope</label>
          <select id="scope-filter" aria-label="Scope" value={scope} onChange={(event) => updateParam("scope", event.target.value, "all")} className="touch-target w-full rounded-lg border border-dark-border bg-dark-bg px-3">
            <option value="all">All Tasks</option><option value="project">Project Tasks</option><option value="test">Test/Examples</option>
          </select>
        </div>
        {handlers ? (
          <p id={REORDER_HELP_ID} className="mt-3 text-xs text-dark-muted">
            Rows are in queue order. Focus a task and press <kbd>Alt</kbd>+<kbd>↑</kbd> or{" "}
            <kbd>Alt</kbd>+<kbd>↓</kbd> to step it through its priority band, or{" "}
            <kbd>Alt</kbd>+<kbd>Home</kbd> and <kbd>Alt</kbd>+<kbd>End</kbd> for the ends.
            Dragging a grip does the same thing.
          </p>
        ) : (
          unavailableReason && <p className="mt-3 text-xs text-dark-muted">{unavailableReason}</p>
        )}
      </section>

      {bandChange && (
        <section
          role="alertdialog"
          aria-label="Confirm a priority change"
          className="rounded-lg border-2 border-yellow-500/50 bg-yellow-950/20 p-4"
        >
          <h2 className="text-sm font-semibold text-yellow-200">
            That drop changes {bandChange.taskId}&apos;s priority as well as its place
          </h2>
          <p className="mt-1 text-xs text-dark-muted">
            It would leave the <strong>{bandChange.from}</strong> band for the{" "}
            <strong>{bandChange.to}</strong> band, landing ahead of {bandChange.before}. How
            urgent something is and where it stands in line are two decisions, so this one is
            asked rather than read off where a finger let go.
          </p>
          <div className="mt-3 flex flex-wrap gap-2">
            <button
              type="button"
              onClick={() => void confirmBandChange()}
              className="touch-target rounded-md bg-yellow-600 px-4 text-sm font-semibold text-white hover:bg-yellow-500"
            >
              Move it to {bandChange.to}
            </button>
            <button
              type="button"
              onClick={() => setBandChange(null)}
              className="touch-target rounded-md border border-dark-border px-4 text-sm text-dark-text hover:bg-dark-border"
            >
              Cancel
            </button>
          </div>
        </section>
      )}

      {notice && (
        // `role="status"` and not `alertdialog`: the move has already landed and is
        // authoritative, so this reports rather than asks. A modal here would put back
        // exactly the friction a post-hoc notice exists to avoid, and there is nothing
        // to confirm -- both buttons are optional, and ignoring it leaves an ordinary
        // human anchor, which is what every un-warned move leaves too.
        <section
          role="status"
          aria-label="What that move did"
          data-testid="queue-move-notice"
          className="rounded-lg border border-yellow-500/50 bg-yellow-950/20 p-4"
        >
          <h2 className="text-sm font-semibold text-yellow-200">
            {notice.taskId} moved. {notice.warnings.length === 1 ? "One thing" : `${notice.warnings.length} things`} worth knowing:
          </h2>
          <ul className="mt-2 space-y-1 text-xs text-dark-muted">
            {notice.warnings.map((warning) => (
              <li key={warning.kind} data-warning={warning.kind}>
                {warning.message}
              </li>
            ))}
          </ul>
          <div className="mt-3 flex flex-wrap gap-2">
            {undoMove(notice.undo) && (
              <button
                type="button"
                onClick={() => void runMove(notice.taskId, undoMove(notice.undo))}
                className="touch-target rounded-md border border-dark-border px-4 text-sm text-dark-text hover:bg-dark-border"
              >
                Undo the move
              </button>
            )}
            <button
              type="button"
              disabled={keeping}
              onClick={() => void keepMove()}
              className="touch-target rounded-md bg-yellow-600 px-4 text-sm font-semibold text-white hover:bg-yellow-500 disabled:opacity-60"
            >
              Keep it here
            </button>
            <button
              type="button"
              onClick={() => setNotice(null)}
              className="touch-target rounded-md px-4 text-sm text-dark-muted hover:text-dark-text"
            >
              Dismiss
            </button>
          </div>
          <p className="mt-2 text-xs text-dark-muted">
            Keeping it records that you read this and decided anyway, which is what stops
            an automatic reorder from moving it back. Dismissing changes nothing.
          </p>
        </section>
      )}

      {moveError && (
        <p role="alert" className="rounded-lg border border-red-500/50 bg-red-950/30 p-3 text-sm text-red-200">
          {moveError}
        </p>
      )}
      {/* Polite and visually hidden. A reorder is often invisible on a filtered or
          grouped list, because the neighbour a task stepped past may not be rendered
          at all -- the sentence is what makes the gesture legible, not decoration. */}
      <output aria-live="polite" className="sr-only">{announcement}</output>

      <section className="overflow-hidden rounded-lg border border-dark-border bg-dark-surface" aria-label="Tasks">
        <ResponsiveTable>
          <thead><tr><th scope="col">Queue</th><th scope="col">Task</th><th scope="col">Status</th><th scope="col">Priority</th><th scope="col">Assigned</th><th scope="col">Updated</th></tr></thead>
          <tbody>
            {visibleRows.map((row) => {
              const movable = movableRow(row.task);
              return (
                <ResponsiveTableRow
                  key={row.task.id}
                  data-task={row.task.id}
                  data-queue-position={row.task.queue_position ?? ""}
                  onKeyDown={(event) => onRowKeyDown(event, row.task)}
                  onDragOver={(event) => { if (movable && dragging && dragging !== row.task.id) event.preventDefault(); }}
                  onDrop={(event) => { event.preventDefault(); onRowDrop(row.task); }}
                >
                  <ResponsiveCell label="Queue" className="text-sm">
                    <div className="flex items-center gap-2">
                      <span className="font-mono text-xs text-dark-muted">{row.task.queue_position ?? "—"}</span>
                      {movable && (
                        <button
                          type="button"
                          id={gripId(row.task.id)}
                          draggable
                          onDragStart={(event) => {
                            // What is being dragged is held in state, not read back out
                            // of the payload: a browser hides `dataTransfer` data during
                            // dragover, which is exactly when the drop target has to
                            // decide whether it will accept.
                            //
                            // The payload is still set, because some browsers will not
                            // start a drag without one -- but under a private type
                            // rather than `text/plain`. As plain text the row could be
                            // dropped into any other application on the machine, which
                            // is not a thing anybody wants a queue position to do. A
                            // type nothing else understands leaves the gesture with
                            // nowhere to deposit itself outside this table.
                            setDragging(row.task.id);
                            if (event.dataTransfer) {
                              event.dataTransfer.effectAllowed = "move";
                              event.dataTransfer.setData(DRAG_TYPE, row.task.id);
                            }
                          }}
                          onDragEnd={() => setDragging(null)}
                          // The name says what this handle is and what it currently
                          // holds. The keys are `aria-keyshortcuts`, which is what that
                          // attribute is for -- a screen reader announces them as
                          // shortcuts, and announces them once, rather than reading a
                          // sentence of instructions on every row a person tabs through.
                          aria-label={`Reorder ${row.task.id}, ${bandOf(row.task)} band, position ${row.task.queue_position}`}
                          aria-keyshortcuts="Alt+ArrowUp Alt+ArrowDown Alt+Home Alt+End"
                          aria-describedby={REORDER_HELP_ID}
                          className="touch-target cursor-grab rounded px-1 text-dark-muted hover:bg-dark-border hover:text-dark-text"
                        >
                          <span aria-hidden="true">⠿</span>
                        </button>
                      )}
                    </div>
                  </ResponsiveCell>
                  <ResponsiveCell label="Task" style={!flattened ? { paddingLeft: `${0.5 + row.depth * 1.5}rem` } : undefined}>
                    <Link to={taskPath(projectId, row.task.id)} className="touch-target block overflow-hidden">
                      <span className="block font-mono text-xs text-blue-400">{row.task.id}</span>
                      <span className="block truncate font-medium text-dark-text">{row.task.title}</span>
                      <span className="block text-xs text-dark-muted">
                        {row.task.category}
                        {flattened && row.ancestors.length > 0 ? ` · part of ${row.ancestors.at(-1)}` : ""}
                      </span>
                    </Link>
                    {!flattened && row.childCount > 0 && (
                      <button type="button" aria-expanded={expanded.has(row.task.id)} onClick={() => toggle(row.task.id)} className="touch-target text-xs text-blue-400 hover:text-blue-300">
                        {expanded.has(row.task.id) ? "▾" : "▸"} {row.childCount} sub-task{row.childCount === 1 ? "" : "s"}{row.openChildren ? `, ${row.openChildren} open` : ""}
                      </button>
                    )}
                  </ResponsiveCell>
                  <ResponsiveCell label="Status"><DependencyState task={row.task} compact /></ResponsiveCell>
                  <ResponsiveCell label="Priority"><span className={`rounded px-2 py-1 text-xs ${PRIORITY_CLASSES[row.task.priority ?? "medium"]}`}>{row.task.priority ?? "medium"}</span></ResponsiveCell>
                  <ResponsiveCell label="Assigned" className="text-sm">{row.task.assignment?.owner ?? "—"}</ResponsiveCell>
                  <ResponsiveCell label="Updated" className="text-sm text-dark-muted"><time dateTime={row.task.updated}>{new Date(row.task.updated).toLocaleString([], { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" })}</time></ResponsiveCell>
                </ResponsiveTableRow>
              );
            })}
          </tbody>
        </ResponsiveTable>
        {tasks.length === 0 && <p className="p-6 text-sm text-dark-muted">No tasks have been created yet.</p>}
        {tasks.length > 0 && visibleRows.length === 0 && <p className="p-6 text-sm text-dark-muted">No tasks match these filters.</p>}
      </section>
    </div>
  );
}
