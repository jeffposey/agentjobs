import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Link, useMatch, useNavigate, useSearchParams } from "react-router-dom";

import type {
  QueueMovePlacement,
  QueueMoveWarning,
  QueueProblemRead,
  TaskRead,
} from "../api/types";
import { DependencyState, dependencyState, STATE_CLASSES } from "./DependencyState";
import { startDragAutoScroll } from "./dragAutoScroll";
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
import {
  buildTaskRows,
  descendantCounts,
  neighbourRow,
  readCollapsed,
  unfoldedRows,
  writeCollapsed,
  type TaskRow,
} from "./taskTree";

/**
 * Which shape this list is rendering in.
 *
 * `table` is the full-width surface it has always been -- six columns, restacking into
 * labelled cards when its own box is narrow. `tree` is the master column task-235's
 * shell puts beside the record: one row per task, folded under its parent, with a
 * selected row and arrow-key movement. They are the same data and the same queue
 * gestures; what differs is that a column 320px wide is picked *from* rather than read
 * across, so the row carries the two signals that decide what to click and the rest
 * moves to the record beside it.
 */
export type TaskListVariant = "table" | "tree";

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
 * Queue, Task, Status, Priority, Assigned, Updated.
 *
 * Five of the six are as wide as the widest thing they will ever hold and no wider: a
 * two-digit position and a grip, a state badge, a priority pill, an owner id, and a
 * timestamp that has to stay on one line to be one line tall. Task takes the rest,
 * which is the only column whose content has no natural width. 39.5rem is spoken for,
 * so Task still gets 176px at the 820px floor of this layout -- narrow, and truncating
 * inside its own column rather than shoving the other five off the screen.
 */
const TASK_COLUMNS = ["5rem", null, "12rem", "6rem", "6rem", "10.5rem"];
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
 * Whether a fresh reader finds the tree open or folded.
 *
 * **Open**, and it is one line to change if that turns out wrong. The alternative --
 * fold every parent by default -- hides most of this backlog behind a click, including
 * whatever `agentjobs next` would hand out, and a queue whose first screen omits the
 * work is not a queue. Folding is therefore an act a reader takes on a specific epic
 * they are done with, which is exactly the thing worth remembering across sessions;
 * `taskTree.ts` stores the folds rather than the unfolds for the same reason. What the
 * spec warned about -- "all-expanded reproduces today's wall of rows" -- is answered by
 * the row rather than by the default: these rows are indented, three lines, and one
 * screen of them is a tree, where today's wall was a flat six-column table.
 */
const FOLDED_BY_DEFAULT = false;

/**
 * Whether this parent is folded, given what the reader has recorded.
 *
 * The stored set holds the *exceptions* to {@link FOLDED_BY_DEFAULT}, so this is the
 * one place the two are combined, and flipping the default flips every fold with it.
 */
function foldedIn(exceptions: Set<string>, taskId: string): boolean {
  return exceptions.has(taskId) !== FOLDED_BY_DEFAULT;
}

/** The stored exceptions with one parent's fold set to `folded`. */
function withFold(exceptions: Set<string>, taskId: string, folded: boolean): Set<string> {
  const next = new Set(exceptions);
  if (folded !== FOLDED_BY_DEFAULT) next.add(taskId);
  else next.delete(taskId);
  return next;
}

/**
 * The paragraph above the table describing the reorder keys.
 *
 * Every handle points at it with `aria-describedby` rather than carrying the
 * instructions in its own name. It is rendered exactly when reordering is available,
 * which is also exactly when a handle exists, so the reference never dangles.
 */
const REORDER_HELP_ID = "queue-reorder-help";
/** The tree's own keys, which exist whether or not anything may be reordered. */
const TREE_HELP_ID = "task-tree-help";

/** The reorder handle's own id, so focus can be put back on it after a step. */
function gripId(taskId: string) {
  return `queue-grip-${taskId}`;
}

/** A row's link, so focus can be put on it after an arrow key moves the selection. */
function rowLinkId(taskId: string) {
  return `task-row-${taskId}`;
}

function taskPath(projectId: string, taskId: string) {
  return `/p/${encodeURIComponent(projectId)}/tasks/${encodeURIComponent(taskId)}`;
}

/** A fingerprint of the order the server last sent, used to expire a prediction. */
function orderSignature(tasks: Array<TaskRead>) {
  return tasks.map((task) => `${task.id}:${task.queue_position ?? ""}`).join("|");
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
 * Bring a row into view **only if some of it is not**.
 *
 * `block: "nearest"` is the whole of that promise: a row already on screen is not
 * moved, so keyboard movement down a visible list does not creep the scrollport, and a
 * row past either edge is brought just inside it rather than centred. Guarded because
 * jsdom implements no scrolling at all and would throw instead of doing nothing.
 */
function revealRow(element: HTMLElement) {
  const row = element.closest("[data-task]");
  if (row && typeof row.scrollIntoView === "function") {
    row.scrollIntoView({ block: "nearest", inline: "nearest" });
  }
}

type PendingMove = { signature: string; tasks: Array<TaskRead> };
type MoveNotice = { taskId: string } & MoveVerdict;
type BandChange = { taskId: string; from: string; to: string; before: string };
/** Where focus goes after the next render, and whether to scroll it into view. */
type FocusTarget = { elementId: string; reveal: boolean };

export function TaskList({
  tasks,
  projectId,
  queueProblems = [],
  reorder = null,
  reorderUnavailable = null,
  variant = "table",
}: {
  tasks: Array<TaskRead>;
  projectId: string;
  queueProblems?: Array<QueueProblemRead>;
  reorder?: ReorderHandlers | null;
  reorderUnavailable?: string | null;
  variant?: TaskListVariant;
}) {
  const tree = variant === "tree";
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  // Which task the record beside this list is showing, read from the route rather than
  // held in state: a deep link, the back button and a click all have to arrive at the
  // same selection, and a refetch must not disturb it. `tasks/new` is a sibling route
  // rendered without this list, but the pattern matches it, so it is excluded here.
  const routeMatch = useMatch("/p/:projectId/tasks/:taskId");
  const routeTaskId = routeMatch?.params.taskId;
  const selectedTaskId = routeTaskId && routeTaskId !== "new" ? routeTaskId : null;
  // The table variant's disclosure: which parents a reader has *opened* on a list whose
  // default is flat. The tree's is the mirror image below -- which parents they have
  // folded on a list whose default is open -- and the two are deliberately separate
  // states rather than one with a flipped sense, because they have different defaults,
  // different persistence and different lifetimes.
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());
  const [collapsed, setCollapsed] = useState<Set<string>>(() => readCollapsed(projectId));
  const [collapsedFor, setCollapsedFor] = useState(projectId);
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
  // What should hold focus after the next render.
  const restoreFocus = useRef<FocusTarget | null>(null);
  // The selection whose ancestors have already been unfolded, so a reader who folds the
  // parent of the task they are reading does not have it spring open again.
  const revealed = useRef<string | null>(null);
  // This list's own root, so a drag can find the box it is scrolling inside.
  const rootRef = useRef<HTMLDivElement>(null);

  // Folds belong to a project, so switching project loads that project's own. Reading
  // during render rather than in an effect: an effect would paint one frame of the
  // previous project's folds over the new project's rows.
  if (collapsedFor !== projectId) {
    setCollapsedFor(projectId);
    setCollapsed(readCollapsed(projectId));
  }

  const search = params.get("q") ?? "";
  const status = filterValue(params, "status", STATUS_FILTERS, "open");
  const priority = filterValue(params, "priority", PRIORITY_FILTERS, "all");
  const scope = filterValue(params, "scope", SCOPE_FILTERS, "all");
  const flattened = search.trim() !== "" || status !== "all" || priority !== "all" || scope !== "all";

  const signature = useMemo(() => orderSignature(tasks), [tasks]);
  const ordered = pending && pending.signature === signature ? pending.tasks : tasks;

  const matching = useMemo(
    () => ordered.filter((task) => matchesTask(task, search, status, priority, scope)),
    [ordered, search, status, priority, scope],
  );
  // Two groupings of the same rows, and the difference is *what is grouped*.
  //
  // The table groups the whole corpus and then hides the rows that do not match, which
  // is why it has to flatten as soon as any filter is set: a matching child under a
  // filtered-out parent would otherwise be indented under a row that is not there. The
  // tree groups the matching tasks instead, so a child whose parent is closed is simply
  // drawn at the root -- which is what lets it stay a tree at the default `open`
  // filter, where the table has always been flat.
  const tableRows = useMemo(() => buildTaskRows(ordered), [ordered]);
  const treeRows = useMemo(() => buildTaskRows(matching), [matching]);
  const rows = tree ? treeRows : tableRows;
  const hidden = useMemo(() => descendantCounts(treeRows), [treeRows]);
  const isFolded = (taskId: string) => foldedIn(collapsed, taskId);
  const visibleRows = tree
    ? unfoldedRows(treeRows, isFolded)
    : tableRows.filter((row) => {
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

  // Scroll the list while a drag is held near the top or bottom of the window.
  //
  // Keyed on `dragging`, so the loop exists only for a drag this list started: a link
  // or a file dragged in from outside never moves the page. `startDragAutoScroll` also
  // tears itself down on drop and dragend, so the loop cannot outlive the gesture even
  // if this state were somehow left set.
  useEffect(() => {
    if (!dragging) return;
    // `within` is what tells the loop which box to move. On the two-region Tasks
    // surface that is the list's own scroll container and the page does not scroll at
    // all; in the stacked shell there is no scrollable ancestor and it falls back to
    // the window, which is what it always did.
    return startDragAutoScroll({ within: rootRef.current });
  }, [dragging]);

  // Put focus back where the last gesture left it.
  //
  // Without this the keyboard reorder works exactly once. React reorders the rows by
  // moving their DOM nodes, and a browser drops focus from a node that is detached and
  // reinserted -- so the second Alt+Down of a two-step reorder either does nothing or,
  // worse, moves whichever task slid into the vacated row. Neither a jsdom test nor a
  // Playwright test that focuses the handle before every press can see this; it was
  // found by pressing the key twice in a real browser.
  //
  // Arrow-key selection uses the same channel for a different reason: the row it lands
  // on has to be the one a further press moves from, and after a route change that row
  // has just been re-rendered.
  useLayoutEffect(() => {
    const target = restoreFocus.current;
    if (!target) return;
    restoreFocus.current = null;
    const element = document.getElementById(target.elementId);
    if (!element) return;
    // A selection scrolls only when the row is off-screen, so focus is told not to
    // scroll and `revealRow` decides. A reorder keeps the browser's own behaviour.
    element.focus(target.reveal ? { preventScroll: true } : undefined);
    if (target.reveal) revealRow(element);
  });

  // A deep link to a folded child unfolds its ancestors.
  //
  // Keyed on the selection *changing*, not on the fold state, so folding the parent of
  // the task you are reading stays folded -- an effect that simply reconciled the two
  // would fight the reader for the control. Nothing happens until the row exists, so a
  // link pasted before the list has loaded is still honoured when it arrives.
  useEffect(() => {
    if (!tree || !selectedTaskId) return;
    if (revealed.current === selectedTaskId) return;
    const row = rows.find((candidate) => candidate.task.id === selectedTaskId);
    if (!row) return;
    revealed.current = selectedTaskId;
    const folded = row.ancestors.filter((ancestor) => foldedIn(collapsed, ancestor));
    if (folded.length === 0) return;
    const next = folded.reduce(
      (carry, ancestor) => withFold(carry, ancestor, false),
      collapsed,
    );
    setCollapsed(next);
    writeCollapsed(projectId, next);
  }, [tree, selectedTaskId, rows, projectId, collapsed]);

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

  /** Fold or unfold one parent, remember it, and say what happened out loud. */
  const setFolded = (row: TaskRow, folded: boolean) => {
    const counts = hidden.get(row.task.id) ?? { total: 0, open: 0 };
    const next = withFold(collapsed, row.task.id, folded);
    setCollapsed(next);
    writeCollapsed(projectId, next);
    setAnnouncement(
      folded
        ? `${row.task.id} folded, hiding ${counts.total} sub-task${counts.total === 1 ? "" : "s"}, ${counts.open} open.`
        : `${row.task.id} unfolded, showing ${counts.total} sub-task${counts.total === 1 ? "" : "s"}.`,
    );
  };

  /**
   * Where a row points, filters included.
   *
   * **The query string is carried across.** Opening a task used to drop it, which was
   * invisible while the list was the whole page and a round trip away from the filter
   * box. Beside the record it is not: an arrow key that quietly emptied the search
   * would re-render the list from three rows to the whole backlog under the reader's
   * hand, and the row they had just moved to would be reinserted somewhere else --
   * taking the focus with it, so the next press did nothing. Found exactly that way,
   * in Chromium, by pressing Down twice.
   */
  const rowTarget = (taskId: string) => ({
    pathname: taskPath(projectId, taskId),
    search: params.toString(),
  });

  /** Move the selection to a row: the record beside the list follows the route. */
  const selectRow = (taskId: string) => {
    restoreFocus.current = { elementId: rowLinkId(taskId), reveal: true };
    navigate(rowTarget(taskId));
  };

  const runMove = async (taskId: string, move: QueueMove | null) => {
    if (!handlers || !move) return;
    setMoveError(null);
    setNotice(null);
    const sequence = (moveCount.current += 1);
    restoreFocus.current = { elementId: gripId(taskId), reveal: false };
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

  const onRowKeyDown = (event: React.KeyboardEvent<HTMLElement>, task: TaskRead) => {
    const direction = STEP_KEYS[event.key];
    if (!event.altKey || !direction || !movableRow(task)) return;
    // Alt+Home and Alt+End would otherwise scroll the page away from the row that just
    // moved, and Alt+Arrow is back/forward in some browsers.
    event.preventDefault();
    void runMove(task.id, stepMove(ordered, task.id, direction));
  };

  /**
   * The tree's own keys, sitting beside the reorder keys without colliding with them.
   *
   * **Alt is the whole of the distinction, and it is checked first.** Up and Down move
   * the selection, Left and Right fold and unfold; the same four keys *with Alt* move
   * the task itself, which is what they already did before this list had a selection.
   * That is why a step gesture is still computed over the band by `queueOrder.ts` and
   * never over the rows on screen: folding a parent changes what a reader can see and
   * changes nothing about where Alt+Up puts a task, so a task can step past a sibling
   * that is not rendered -- and the live region says whose place it took, which is the
   * only part of the gesture folding could otherwise have made invisible.
   */
  const onTreeKeyDown = (event: React.KeyboardEvent<HTMLElement>, row: TaskRow) => {
    if (event.altKey) {
      onRowKeyDown(event, row.task);
      return;
    }
    if (event.ctrlKey || event.metaKey || event.shiftKey) return;
    const foldable = (hidden.get(row.task.id)?.total ?? 0) > 0;
    const folded = isFolded(row.task.id);

    const move = (target: TaskRow | null | undefined) => {
      if (!target) return;
      event.preventDefault();
      selectRow(target.task.id);
    };

    switch (event.key) {
      case "ArrowDown":
        return move(neighbourRow(visibleRows, row.task.id, 1));
      case "ArrowUp":
        return move(neighbourRow(visibleRows, row.task.id, -1));
      case "Home":
        return move(visibleRows[0]);
      case "End":
        return move(visibleRows[visibleRows.length - 1]);
      case "ArrowRight":
        // Open it, or -- already open -- step into it, which is what a tree does.
        if (foldable && folded) {
          event.preventDefault();
          setFolded(row, false);
          return;
        }
        if (foldable) return move(neighbourRow(visibleRows, row.task.id, 1));
        return;
      case "ArrowLeft":
        // Close it, or -- already closed, or a leaf -- step out to its parent.
        if (foldable && !folded) {
          event.preventDefault();
          setFolded(row, true);
          return;
        }
        return move(visibleRows.find((candidate) => candidate.task.id === row.ancestors.at(-1)));
      default:
        return;
    }
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

  /**
   * The grip, identical in both shapes.
   *
   * A drop onto a *folded* parent is an ordinary drop and deliberately nothing more:
   * it places the dragged task beside that parent in its band. It does not reparent --
   * that is a real feature and its own task -- and it does not unfold on hover, which
   * would move every row under the pointer mid-gesture and land the drop somewhere
   * nobody aimed at. So the answer to "what does dropping on a folded epic do" is "the
   * same thing as dropping on any other row", which is a decision rather than an
   * accident.
   */
  const renderGrip = (task: TaskRead) => (
    <button
      type="button"
      id={gripId(task.id)}
      draggable
      onDragStart={(event) => {
        // What is being dragged is held in state, not read back out of the payload: a
        // browser hides `dataTransfer` data during dragover, which is exactly when the
        // drop target has to decide whether it will accept.
        //
        // The payload is still set, because some browsers will not start a drag
        // without one -- but under a private type rather than `text/plain`. As plain
        // text the row could be dropped into any other application on the machine,
        // which is not a thing anybody wants a queue position to do. A type nothing
        // else understands leaves the gesture with nowhere to deposit itself outside
        // this list.
        setDragging(task.id);
        if (event.dataTransfer) {
          event.dataTransfer.effectAllowed = "move";
          event.dataTransfer.setData(DRAG_TYPE, task.id);
        }
      }}
      onDragEnd={() => setDragging(null)}
      // The name says what this handle is and what it currently holds. The keys are
      // `aria-keyshortcuts`, which is what that attribute is for -- a screen reader
      // announces them as shortcuts, and announces them once, rather than reading a
      // sentence of instructions on every row a person tabs through.
      aria-label={`Reorder ${task.id}, ${bandOf(task)} band, position ${task.queue_position}`}
      aria-keyshortcuts="Alt+ArrowUp Alt+ArrowDown Alt+Home Alt+End"
      aria-describedby={REORDER_HELP_ID}
      className="touch-target cursor-grab rounded px-1 text-dark-muted hover:bg-dark-border hover:text-dark-text"
    >
      <span aria-hidden="true">⠿</span>
    </button>
  );

  const dragProps = (task: TaskRead) => ({
    onDragOver: (event: React.DragEvent) => {
      if (movableRow(task) && dragging && dragging !== task.id) event.preventDefault();
    },
    onDrop: (event: React.DragEvent) => {
      event.preventDefault();
      onRowDrop(task);
    },
  });

  const treeBody = (
    <ul className="divide-y divide-dark-border">
      {visibleRows.map((row) => {
        const task = row.task;
        const state = dependencyState(task);
        const counts = hidden.get(task.id) ?? { total: 0, open: 0 };
        const folded = isFolded(task.id);
        const selected = task.id === selectedTaskId;
        // A child whose parent did not survive the filter is drawn at the root, so the
        // row says where it came from rather than silently losing its place.
        const orphanedFrom = row.depth === 0 && task.parent ? task.parent : null;
        return (
          <li
            key={task.id}
            data-task={task.id}
            data-queue-position={task.queue_position ?? ""}
            data-depth={row.depth}
            data-selected={selected ? "true" : undefined}
            onKeyDown={(event) => onTreeKeyDown(event, row)}
            {...dragProps(task)}
            // The indent stops growing at four levels: past that it is eating the title
            // in a 320px column to draw a depth nobody is counting.
            style={{ paddingLeft: `${0.25 + Math.min(row.depth, 4) * 0.85}rem` }}
            className={`flex gap-1 py-1 pr-2 ${selected ? "bg-blue-950/60" : "hover:bg-dark-bg/60"}`}
          >
            <div className="flex shrink-0 items-start">
              {movableRow(task) ? renderGrip(task) : <span className="inline-block w-5" />}
              {counts.total > 0 ? (
                <button
                  type="button"
                  aria-expanded={!folded}
                  aria-label={`${folded ? "Unfold" : "Fold"} ${task.id}, ${counts.total} sub-task${counts.total === 1 ? "" : "s"}, ${counts.open} open`}
                  aria-keyshortcuts="ArrowLeft ArrowRight"
                  onClick={() => setFolded(row, !folded)}
                  className="touch-target rounded px-1 text-dark-muted hover:bg-dark-border hover:text-dark-text"
                >
                  <span aria-hidden="true">{folded ? "▸" : "▾"}</span>
                </button>
              ) : (
                <span className="inline-block w-5" />
              )}
            </div>
            <div className="min-w-0 flex-1 py-1">
              <Link
                id={rowLinkId(task.id)}
                to={rowTarget(task.id)}
                // Announced, not merely coloured: a reader who cannot see the highlight
                // is told which row the record beside the list belongs to.
                aria-current={selected ? "page" : undefined}
                className="block overflow-hidden"
              >
                <span className="flex items-baseline gap-2">
                  <span className="font-mono text-xs text-blue-400">{task.id}</span>
                  {orphanedFrom && (
                    <span className="truncate text-xs text-dark-muted">part of {orphanedFrom}</span>
                  )}
                  <span
                    data-field="queue"
                    className="ml-auto shrink-0 font-mono text-xs text-dark-muted"
                  >
                    {task.queue_position ?? "—"}
                  </span>
                </span>
                {/* The title is the one line that gets cut, and the full text stays in
                    the tooltip and on the record this row opens. */}
                <span className="block truncate font-medium text-dark-text" title={task.title}>
                  {task.title}
                </span>
              </Link>
              <div className="mt-1 flex flex-wrap items-center gap-1" data-field="status">
                <span
                  className={`inline-flex rounded border px-1.5 text-xs font-medium ${STATE_CLASSES[state.kind]}`}
                >
                  {state.label}
                </span>
                <span className={`rounded px-1.5 text-xs ${PRIORITY_CLASSES[task.priority ?? "medium"]}`}>
                  {task.priority ?? "medium"}
                </span>
                {/* A fold must not hide work silently. The count is on the row itself,
                    not only inside the control's accessible name, so a reader scanning
                    a folded backlog can see that nine open tasks are under this one. */}
                {folded && counts.total > 0 && (
                  <span className="rounded border border-dark-border px-1.5 text-xs text-dark-muted">
                    {counts.total} folded, {counts.open} open
                  </span>
                )}
              </div>
            </div>
          </li>
        );
      })}
    </ul>
  );

  const tableBody = (
    <ResponsiveTable columns={TASK_COLUMNS} stackWhenNarrow>
      <thead><tr><th scope="col">Queue</th><th scope="col">Task</th><th scope="col">Status</th><th scope="col">Priority</th><th scope="col">Assigned</th><th scope="col">Updated</th></tr></thead>
      <tbody>
        {visibleRows.map((row) => (
          <ResponsiveTableRow
            key={row.task.id}
            data-task={row.task.id}
            data-queue-position={row.task.queue_position ?? ""}
            onKeyDown={(event) => onRowKeyDown(event, row.task)}
            {...dragProps(row.task)}
          >
            <ResponsiveCell label="Queue" data-field="queue" className="text-sm">
              <div className="flex items-center gap-2">
                <span className="font-mono text-xs text-dark-muted">{row.task.queue_position ?? "—"}</span>
                {movableRow(row.task) && renderGrip(row.task)}
              </div>
            </ResponsiveCell>
            <ResponsiveCell label="Task" style={!flattened ? { paddingLeft: `${0.5 + row.depth * 1.5}rem` } : undefined}>
              <Link to={rowTarget(row.task.id)} className="touch-target block overflow-hidden">
                <span className="block font-mono text-xs text-blue-400">{row.task.id}</span>
                {/* The title is the one line that gets cut, so it is the one that
                    needs somewhere to say the rest. The id and the category below
                    wrap instead: they are short, and truncating them would cut
                    the mobile cards too, where there is no column to protect. */}
                <span className="block truncate font-medium text-dark-text" title={row.task.title}>{row.task.title}</span>
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
            <ResponsiveCell label="Status" data-field="status"><DependencyState task={row.task} compact /></ResponsiveCell>
            <ResponsiveCell label="Priority"><span className={`rounded px-2 py-1 text-xs ${PRIORITY_CLASSES[row.task.priority ?? "medium"]}`}>{row.task.priority ?? "medium"}</span></ResponsiveCell>
            <ResponsiveCell label="Assigned" className="text-sm">{row.task.assignment?.owner ?? "—"}</ResponsiveCell>
            <ResponsiveCell label="Updated" className="whitespace-nowrap text-sm text-dark-muted"><time dateTime={row.task.updated}>{new Date(row.task.updated).toLocaleString([], { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" })}</time></ResponsiveCell>
          </ResponsiveTableRow>
        ))}
      </tbody>
    </ResponsiveTable>
  );

  return (
    <div className={tree ? "space-y-4" : "space-y-6"} ref={rootRef}>
      {/* `@container`, so the row below asks its own box rather than the window whether
          there is room for one line. The four controls have a combined minimum of
          43rem, so the viewport rule this replaces put them in a row inside a 350px
          region and hung a horizontal scrollbar off the whole list. The question was
          always about this box; until task-237 the box was the window. task-356
          replaces the row with a filter button, at which point this goes away. */}
      <section
        className="@container rounded-lg border border-dark-border bg-dark-surface p-4"
        aria-label="Task filters"
      >
        <div className="grid gap-3 @min-[43rem]:grid-cols-[minmax(16rem,1fr)_repeat(3,minmax(9rem,auto))]">
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
        {tree && (
          <p id={TREE_HELP_ID} className="mt-3 text-xs text-dark-muted">
            <kbd>↑</kbd> and <kbd>↓</kbd> move the selection and open that task beside the
            list; <kbd>←</kbd> and <kbd>→</kbd> fold and unfold a parent. A fold is
            remembered for this project.
          </p>
        )}
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
          folded list, because the neighbour a task stepped past may not be rendered
          at all -- the sentence is what makes the gesture legible, not decoration. A
          fold announces itself here too, for the same reason. */}
      <output aria-live="polite" className="sr-only">{announcement}</output>

      <section className="overflow-hidden rounded-lg border border-dark-border bg-dark-surface" aria-label="Tasks">
        {tree ? treeBody : tableBody}
        {tasks.length === 0 && <p className="p-6 text-sm text-dark-muted">No tasks have been created yet.</p>}
        {tasks.length > 0 && visibleRows.length === 0 && <p className="p-6 text-sm text-dark-muted">No tasks match these filters.</p>}
      </section>
    </div>
  );
}
