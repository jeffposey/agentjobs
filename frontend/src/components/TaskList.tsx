import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Link, useMatch, useNavigate, useSearchParams } from "react-router-dom";

import type {
  QueueMovePlacement,
  QueueMoveWarning,
  QueueProblemRead,
  TaskSummaryRead,
} from "../api/types";
import { ArchivedTag, DependencyState, dependencyState } from "./DependencyState";
import { PriorityMark, PRIORITY_COLOURS, priorityName } from "./PriorityMark";
import { STATUSES, StatusChip } from "./StatusChip";
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

const STATUS_FILTERS = new Set(["all", "open", "attention", "draft", "ready", "active", "finishing", "human", "external", "reset", "closed"]);
const PRIORITY_FILTERS = new Set(["all", "critical", "high", "medium", "low"]);
const SCOPE_FILTERS = new Set(["all", "project", "test"]);
/**
 * The band header a sidebar row sits under, or null when it continues the group above.
 *
 * Only a root row can open a group: a child stays under its parent whatever its own
 * priority (task-563), so it is governed by its root's header. Open and closed work
 * are separate groups because the server lists every open band before any closed one
 * (`listing_key`), so a filter showing both would otherwise draw CRITICAL TASKS twice.
 */
export function bandHeaders(rows: Array<TaskRow>): Map<string, { band: string; closed: boolean }> {
  const headers = new Map<string, { band: string; closed: boolean }>();
  let previous: string | null = null;
  for (const row of rows) {
    if (row.depth !== 0) continue;
    const closed = row.task.lifecycle === "closed";
    const band = priorityName(row.task.priority);
    const key = `${closed}:${band}`;
    if (key !== previous) headers.set(row.task.id, { band, closed });
    previous = key;
  }
  return headers;
}
/**
 * Queue, Task, Status, Priority, Assigned, Updated.
 *
 * Five of the six are as wide as the widest thing they will ever hold and no wider: a
 * two-digit position and a grip, a state badge, a priority mark, an owner id, and a
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
 * The text describing the reorder keys, behind the `?` button beside the filters.
 *
 * Every handle points at it with `aria-describedby` rather than carrying the
 * instructions in its own name. It is rendered whenever reordering is available, which
 * is also exactly when a handle exists, so the reference never dangles -- and it stays
 * in the document while the popover is closed, visually hidden rather than unmounted,
 * so the description a screen reader gets does not depend on whether somebody hovered.
 */
const REORDER_HELP_ID = "queue-reorder-help";
/** The `?` button, so Escape can put focus back on it. */
const HELP_BUTTON_ID = "task-help-button";

/** The filter button, so Escape can put focus back where the popover was opened from. */
const FILTER_BUTTON_ID = "task-filter-button";
/** The popover's own id, named by `aria-controls` exactly while it exists. */
const FILTER_POPOVER_ID = "task-filter-popover";

/**
 * The three selects behind the button, with the value each is at when nothing is set.
 *
 * One list read twice: it is what the closed button counts, and it is what "clear"
 * clears. The point of the single list is that a fourth filter -- task-156's
 * difficulty, when it lands -- is one entry rather than several edits that can end up
 * disagreeing with each other.
 */
const POPOVER_FILTERS = [
  { key: "status", label: "Status", fallback: "open" },
  { key: "priority", label: "Priority", fallback: "all" },
  { key: "scope", label: "Scope", fallback: "all" },
] as const;

/**
 * Which filters the closed button has to confess to, as words.
 *
 * Given the **resolved** values rather than the raw parameters, because `filterValue()`
 * ignores a value outside its allowed set: `?status=nonsense` filters nothing, and a
 * badge counting it would be reporting a filter that is not being applied.
 *
 * The search box is deliberately **not** counted. It stays on screen with its text in
 * it, so it says what it is doing without help; counting it would light the badge for a
 * filter the reader can already see, which is the noise that makes an indicator stop
 * being read.
 */
function activeFilterSummary(current: Record<string, string>): Array<string> {
  return POPOVER_FILTERS.flatMap(({ key, label, fallback }) =>
    current[key] !== fallback ? [`${label} ${current[key]}`] : [],
  );
}

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
function orderSignature(tasks: Array<TaskSummaryRead>) {
  return tasks.map((task) => `${task.id}:${task.queue_position ?? ""}`).join("|");
}

/**
 * Whether one task answers the Status filter.
 *
 * Three of the options are not a `lifecycle` or a `ball` value, because the state a
 * reader wants to separate is not either of those. A park on a usage limit and a third
 * party being down are both `external`, and only one of them needs anybody: `reset`
 * selects the self-clearing waits and `external` now excludes them, so each is reachable
 * on its own. Before this they shared one option and a reader could filter to neither.
 *
 * `finishing` is the third and the same shape again (task-509): a task whose branch is
 * being merged is `active`/`agent`/`work`, exactly like a task an agent is editing, so
 * `active` cannot separate them. It selects on `live_finish` -- the structure -- rather
 * than on the word in `display_status`, which is what ENGINEERING.md's rendered-value
 * rule asks for and what stops the filter breaking the day the label is reworded.
 */
function matchesStatus(task: TaskSummaryRead, status: string, waiting: ReadonlySet<string>) {
  const selfClearing = task.self_clearing_wait != null;
  if (status === "all") return true;
  if (status === "open") return task.lifecycle !== "closed";
  if (status === "attention") return waiting.has(task.id);
  if (status === "reset") return selfClearing;
  if (status === "finishing") return task.live_finish != null;
  if (status === "external") return task.ball === "external" && !selfClearing;
  return task.lifecycle === status || task.ball === status;
}

function matchesTask(
  task: TaskSummaryRead,
  search: string,
  status: string,
  priority: string,
  scope: string,
  waiting: ReadonlySet<string>,
) {
  const term = search.trim().toLowerCase();
  // The id is searched as well as the title because the id is what people quote:
  // "058" and "task-058" both have to find task-058-multi-project-gui. Summary and
  // description are deliberately left out -- this box filters a visible list, and a
  // row matching on text the row does not show reads as a bug. The API's /api/search
  // is the full-text surface.
  const titleMatches = term === ""
    || task.title.toLowerCase().includes(term)
    || task.id.toLowerCase().includes(term);
  const statusMatches = matchesStatus(task, status, waiting);
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

const EMPTY_WAITING: ReadonlySet<string> = new Set();

type PendingMove = { signature: string; tasks: Array<TaskSummaryRead> };
type MoveNotice = { taskId: string } & MoveVerdict;
type BandChange = { taskId: string; from: string; to: string; before: string };
/** Which side of a row the drop under way would land on. */
type DropSide = "before" | "after";
/** The row an insertion line is currently drawn on, and which edge of it. */
type DropTarget = { id: string; side: DropSide };
/** Where focus goes after the next render, and whether to scroll it into view. */
type FocusTarget = { elementId: string; reveal: boolean };

export function TaskList({
  tasks,
  projectId,
  queueProblems = [],
  reorder = null,
  reorderUnavailable = null,
  variant = "table",
  waitingOnYou = EMPTY_WAITING,
}: {
  tasks: Array<TaskSummaryRead>;
  projectId: string;
  queueProblems?: Array<QueueProblemRead>;
  reorder?: ReorderHandlers | null;
  reorderUnavailable?: string | null;
  variant?: TaskListVariant;
  /**
   * The attention episode's members: everything work has stopped on, waiting on you.
   *
   * A prop rather than a query of this component's own, because half of the set is
   * derived from the machine's run ledger and none of it is readable from a row
   * (task-499) -- and because the page above already holds the attention answer that
   * draws the header badge, so fetching it twice would give the badge and the filter
   * two chances to disagree about the same number.
   */
  waitingOnYou?: ReadonlySet<string>;
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
  // The row the pointer is over and the side of it the drop would use, so the list can
  // draw an insertion line there. State rather than a CSS `:hover` rule, because the
  // side is not a fact about the pointer: it depends on which way the dragged task is
  // travelling, and only `dropSide` knows that.
  const [dropTarget, setDropTarget] = useState<DropTarget | null>(null);
  // What should hold focus after the next render.
  const restoreFocus = useRef<FocusTarget | null>(null);
  // The selection whose ancestors have already been unfolded, so a reader who folds the
  // parent of the task they are reading does not have it spring open again.
  const revealed = useRef<string | null>(null);
  // This list's own root, so a drag can find the box it is scrolling inside.
  const rootRef = useRef<HTMLDivElement>(null);
  // Whether the filter popover is open. Component state and not the URL: which controls
  // are on screen is not a property of the list a pasted link should reproduce, and a
  // popover that reopened itself on every refetch would be its own defect.
  const [filtersOpen, setFiltersOpen] = useState(false);
  // The button and the popover together, so an outside click can be told from an
  // inside one -- clicking the button while it is open must toggle rather than close
  // and immediately reopen.
  const filtersRef = useRef<HTMLDivElement>(null);
  const firstFilterRef = useRef<HTMLSelectElement>(null);
  // The keyboard help behind the `?` button (task-385). Two sources, because a tooltip
  // and a tap are different gestures: `helpPeek` is a mouse resting on the button, and
  // closes when it leaves; `helpPinned` is a click or a tap, and stays until dismissed.
  // A touch screen has no hover, so only the pin ever opens it there.
  const [helpPinned, setHelpPinned] = useState(false);
  const [helpPeek, setHelpPeek] = useState(false);
  const helpShown = helpPinned || helpPeek;
  const helpRef = useRef<HTMLDivElement>(null);

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
  const activeFilters = activeFilterSummary({ status, priority, scope });
  const anyFilterSet = activeFilters.length > 0 || search !== "";

  const signature = useMemo(() => orderSignature(tasks), [tasks]);
  const ordered = pending && pending.signature === signature ? pending.tasks : tasks;

  const matching = useMemo(
    () => ordered.filter((task) => matchesTask(task, search, status, priority, scope, waitingOnYou)),
    [ordered, search, status, priority, scope, waitingOnYou],
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
        if (!matchesTask(row.task, search, status, priority, scope, waitingOnYou)) return false;
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

  /**
   * Back to the defaults, in one gesture and one navigation.
   *
   * The search box is cleared too. It is outside the popover, but it is a filter, and a
   * "clear" that leaves a list still hiding most of it is the thing this whole task
   * exists to prevent. One `setParams` rather than four `updateParam` calls: each of
   * those reads `params` from this render, so four in a row would each undo the last.
   */
  const clearFilters = () => {
    const next = new URLSearchParams(params);
    next.delete("q");
    for (const { key } of POPOVER_FILTERS) next.delete(key);
    setParams(next, { replace: true });
  };

  // Opening moves focus into the popover, which is what makes it reachable at all from
  // the keyboard: a `Space` on the button that left focus behind would leave a reader
  // pressing Tab through the whole list to find three selects that just appeared.
  useEffect(() => {
    if (filtersOpen) firstFilterRef.current?.focus();
  }, [filtersOpen]);

  // The three things a hand-rolled popover forgets. Escape and an outside click both
  // close it; only Escape moves focus, because an outside click has a target of its own
  // and stealing focus back to the button would fight the thing being clicked.
  useEffect(() => {
    if (!filtersOpen) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.stopPropagation();
      setFiltersOpen(false);
      document.getElementById(FILTER_BUTTON_ID)?.focus();
    };
    const onPointerDown = (event: MouseEvent) => {
      if (filtersRef.current?.contains(event.target as Node)) return;
      setFiltersOpen(false);
    };
    document.addEventListener("keydown", onKey);
    document.addEventListener("mousedown", onPointerDown);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("mousedown", onPointerDown);
    };
  }, [filtersOpen]);

  // The same two dismissals for the help. Escape returns focus to the button only when
  // focus was inside, since a peek never took it anywhere.
  useEffect(() => {
    if (!helpShown) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      const hadFocus = helpRef.current?.contains(document.activeElement) ?? false;
      setHelpPinned(false);
      setHelpPeek(false);
      if (hadFocus) document.getElementById(HELP_BUTTON_ID)?.focus();
    };
    const onPointerDown = (event: MouseEvent) => {
      if (helpRef.current?.contains(event.target as Node)) return;
      setHelpPinned(false);
      setHelpPeek(false);
    };
    document.addEventListener("keydown", onKey);
    document.addEventListener("mousedown", onPointerDown);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("mousedown", onPointerDown);
    };
  }, [helpShown]);

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
   * Where a row points, filters included -- **in the tree only**.
   *
   * Opening a task drops the query string, which was invisible while the list was the
   * whole page: the list unmounted anyway, and Back put the filters straight back.
   * Beside the record it is neither. The list stays mounted, so an arrow key that
   * quietly emptied the search re-renders it from three rows to the whole backlog under
   * the reader's hand, React reinserts the row they had just moved to somewhere else,
   * and the browser drops focus from the node it moved -- so the next press does
   * nothing. That is task-207's defect reached through a different door, and it was
   * found the same way task-207's was: by pressing the key twice in Chromium.
   *
   * The table keeps the bare path it has always had. It has no selection to lose and
   * no list left on screen to re-render, and this task's constraint is that below the
   * device-class threshold the list renders as it does today.
   */
  const rowTarget = (taskId: string) =>
    tree ? { pathname: taskPath(projectId, taskId), search: params.toString() } : taskPath(projectId, taskId);

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
  const movableRow = (task: TaskSummaryRead) =>
    Boolean(handlers) && isInQueue(task) && !brokenBands.has(bandOf(task));

  const onRowKeyDown = (event: React.KeyboardEvent<HTMLElement>, task: TaskSummaryRead) => {
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

  /**
   * Which edge of `task` the drag under way would land on, or `null` if this row would
   * not take it at all.
   *
   * One function, two callers: the insertion line is drawn from it and the drop is
   * decided by it. The alternative -- an indicator with its own idea of which side it
   * is on -- can only ever be right by coincidence, and a reader who aims at a line
   * that lies has been given worse feedback than no line.
   *
   * A cross-band hover answers `before`, which is the position the band-change prompt
   * offers; the prompt still runs it, and this says nothing about whether it is taken.
   */
  const dropSide = (task: TaskSummaryRead): DropSide | null => {
    if (!handlers || !dragging || dragging === task.id || !movableRow(task)) return null;
    const source = ordered.find((candidate) => candidate.id === dragging);
    if (!source || !movableRow(source)) return null;
    if (bandOf(source) !== bandOf(task)) return "before";
    const band = bandMembers(ordered, bandOf(source));
    const from = band.findIndex((candidate) => candidate.id === source.id);
    const to = band.findIndex((candidate) => candidate.id === task.id);
    return from < to ? "after" : "before";
  };

  const onRowDrop = (task: TaskSummaryRead) => {
    const side = dropSide(task);
    const sourceId = dragging;
    setDragging(null);
    setDropTarget(null);
    if (!sourceId || !side) return;
    const source = ordered.find((candidate) => candidate.id === sourceId);
    if (!source) return;
    if (bandOf(source) !== bandOf(task)) {
      // Two decisions in one gesture -- where it stands, and how urgent it is. The
      // second is asked out loud rather than inferred from where a finger let go.
      setBandChange({ taskId: source.id, from: bandOf(source), to: bandOf(task), before: task.id });
      return;
    }
    void runMove(source.id, side === "after" ? { after: task.id } : { before: task.id });
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
  const renderGrip = (task: TaskSummaryRead) => (
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
          // The browser's default drag image is a picture of whatever carries
          // `draggable`, and that is this handle: a 20px glyph floating over a list
          // where nothing else has changed. What is moving is the row, so the row is
          // what follows the pointer. The snapshot is taken synchronously here, before
          // React re-renders the source row faded, so the ghost is a picture of the row
          // as it reads rather than of the hole it leaves.
          const row = event.currentTarget.closest<HTMLElement>("[data-task]");
          if (row && typeof event.dataTransfer.setDragImage === "function") {
            const box = row.getBoundingClientRect();
            event.dataTransfer.setDragImage(row, event.clientX - box.left, event.clientY - box.top);
          }
        }
      }}
      onDragEnd={() => {
        setDragging(null);
        setDropTarget(null);
      }}
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

  /**
   * What every row needs to take part in a drag, in both list shapes.
   *
   * The two `data-` attributes are the whole of the feedback: `styles.css` fades the
   * row in flight and draws the insertion line, so a `<li>` and a `<tr>` say the same
   * thing without either of them carrying a class list the other does not.
   *
   * There is no `onDragLeave`. `dragleave` bubbles out of a row's own children, so
   * clearing on it makes the line flicker every time the pointer crosses the title
   * link; hovering a row that will not take the drop clears it instead, and `dragend`
   * clears it when the gesture leaves the list altogether.
   */
  const dragProps = (task: TaskSummaryRead) => ({
    onDragOver: (event: React.DragEvent) => {
      const side = dropSide(task);
      if (!side) {
        setDropTarget((current) => (current === null ? current : null));
        return;
      }
      event.preventDefault();
      setDropTarget((current) =>
        current && current.id === task.id && current.side === side
          ? current
          : { id: task.id, side },
      );
    },
    onDrop: (event: React.DragEvent) => {
      event.preventDefault();
      onRowDrop(task);
    },
    "data-dragging": dragging === task.id ? "true" : undefined,
    "data-drop-side": dropTarget?.id === task.id ? dropTarget.side : undefined,
  });

  // Priority is where a row sits, not a chip on it (task-563): the list is already in
  // band order, so a header opens each band and governs every row until the next one.
  const headers = bandHeaders(visibleRows);
  // The band a row is drawn under, so its left edge carries the header's colour down
  // the whole group (owner's revision, task-563). A child inherits its root's band.
  let rowBand = "medium";
  const treeBody = (
    <ul className="divide-y divide-dark-border">
      {visibleRows.flatMap((row) => {
        const task = row.task;
        const header = headers.get(task.id);
        if (header) rowBand = header.band;
        else if (row.depth === 0) rowBand = priorityName(task.priority);
        // A divider, not a task: no link, no grip, no drag handlers and no key handler,
        // and it is not in `visibleRows`, so arrow keys and the count never see it.
        const headerRow = header ? (
          <li
            key={`band-${header.closed ? "closed" : "open"}-${header.band}-${task.id}`}
            data-band-header={header.band}
            // No edge of its own (task-576): the colour runs down the rows, and boxing
            // the header text too was one stripe more than the band needed. The edge's
            // width is added to the padding instead, so the text sits where it sat.
            className="bg-dark-bg pb-1 pr-2 pt-3 pl-[calc(3px+0.5rem)]"
          >
            <h3 className="m-0 text-xs">
              <PriorityMark
                priority={header.band}
                suffix={header.closed ? " TASKS · CLOSED" : " TASKS"}
                style={header.closed ? { opacity: 0.7 } : undefined}
              />
            </h3>
          </li>
        ) : null;
        const state = dependencyState(task);
        const counts = hidden.get(task.id) ?? { total: 0, open: 0 };
        const folded = isFolded(task.id);
        const selected = task.id === selectedTaskId;
        // A child whose parent did not survive the filter is drawn at the root, so the
        // row says where it came from rather than silently losing its place.
        const orphanedFrom = row.depth === 0 && task.parent ? task.parent : null;
        const taskRow = (
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
            data-band={rowBand}
            style={{
              paddingLeft: `${0.25 + Math.min(row.depth, 4) * 1.1}rem`,
              borderLeftColor: PRIORITY_COLOURS[priorityName(rowBand)],
            }}
            className={`flex gap-1 border-l-[3px] py-1 pr-2 ${selected ? "bg-blue-950/60" : "hover:bg-dark-bg/60"}`}
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
                {/* The raw `queue_position` is deliberately not on this row (task-362).
                    It is a gapped band coordinate rather than an ordinal, so a reader
                    who tries to act on it is misled either way, and the list's own
                    order already carries the ordering exactly. It stays where it
                    informs something: the grip's accessible name, which is how a
                    keyboard reorder is announced, and `NextExplanation`, which says
                    "position X of the Y" in a sentence that explains it. This
                    partially reverses task-207, which shipped the ordering (kept) and
                    the coordinate behind it (removed). */}
                {/* The id line carries the status chip beside the id (task-563), and
                    never wraps: at phone width the chip and the tags after it are
                    clipped before the line is allowed to break. There is no priority
                    here -- the band header above says it. */}
                <span className="flex min-w-0 flex-nowrap items-center gap-1.5 overflow-hidden" data-field="id-line">
                  <span className="shrink-0 font-mono text-xs text-blue-400">{task.id}</span>
                  {/* The chip is about 20% smaller here than elsewhere (owner's revision):
                      at full size it outweighed the id and title it sits between. Scoped
                      to this row so StatusChip itself is unchanged. */}
                  <span
                    className="flex min-w-0 shrink items-center gap-1 overflow-hidden [&>[data-status-category]]:px-1.5 [&>[data-status-category]]:py-0 [&>[data-status-category]]:text-[0.6rem] [&>[data-status-category]]:leading-4"
                    data-field="status"
                  >
                    <StatusChip category={state.category} label={state.label} motion={state.motion} />
                    {task.archived && <ArchivedTag />}
                  </span>
                  {/* A fold must not hide work silently. The count is on the row
                      itself, not only inside the control's accessible name, so a reader
                      scanning a folded backlog can see that nine open tasks are under
                      this one. */}
                  {folded && counts.total > 0 && (
                    <span className="min-w-0 truncate rounded border border-dark-border px-1.5 text-xs text-dark-muted">
                      {counts.total} folded, {counts.open} open
                    </span>
                  )}
                  {orphanedFrom && (
                    <span className="min-w-0 truncate text-xs text-dark-muted">part of {orphanedFrom}</span>
                  )}
                </span>
                {/* Two lines of title, then an ellipsis -- the line the chips used to
                    take. The full text stays in the tooltip and on the record. Normal
                    weight (task-576): a list where every title is heavy has no emphasis
                    left. The detail panel's title is the headline and stays bold. */}
                <span
                  className="mt-0.5 line-clamp-2 break-words font-normal leading-snug text-dark-text"
                  data-field="title"
                  title={task.title}
                >
                  {task.title}
                </span>
              </Link>
            </div>
          </li>
        );
        return headerRow ? [headerRow, taskRow] : [taskRow];
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
            <ResponsiveCell label="Priority"><PriorityMark priority={row.task.priority} /></ResponsiveCell>
            <ResponsiveCell label="Assigned" className="text-sm">{row.task.assignment?.owner ?? "—"}</ResponsiveCell>
            <ResponsiveCell label="Updated" className="whitespace-nowrap text-sm text-dark-muted"><time dateTime={row.task.updated}>{new Date(row.task.updated).toLocaleString([], { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" })}</time></ResponsiveCell>
          </ResponsiveTableRow>
        ))}
      </tbody>
    </ResponsiveTable>
  );

  return (
    <div className={tree ? "space-y-4" : "space-y-6"} ref={rootRef}>
      {/* One line, at every width. What used to be here was a bordered card holding
          four full-width controls, which is 200px of a phone's first screen and most
          of the sidebar's -- spent permanently on selects whose defaults almost every
          visit uses. The search box stays out here and the three selects moved behind
          the button; the reasoning is on task-356 as a decision. */}
      <section className="space-y-3" aria-label="Task filters">
        <div className="flex items-center gap-2">
          <label className="sr-only" htmlFor="task-search">Search tasks</label>
          <input
            id="task-search"
            type="search"
            value={search}
            onChange={(event) => updateParam("q", event.target.value, "")}
            placeholder="Search title or id (e.g. 058)"
            className="touch-target min-w-0 flex-1 rounded-lg border border-dark-border bg-dark-bg px-4 text-dark-text focus:border-blue-500 focus:outline-none"
          />
          <div className="relative shrink-0" ref={filtersRef}>
            <button
              type="button"
              id={FILTER_BUTTON_ID}
              aria-expanded={filtersOpen}
              aria-haspopup="dialog"
              aria-controls={filtersOpen ? FILTER_POPOVER_ID : undefined}
              // The name carries what the badge shows, in words, so the state is
              // announced rather than only drawn. Both halves are required: a screen
              // reader gets the sentence, everyone else gets the count without opening
              // anything -- a list quietly hiding two thirds of the backlog must not
              // look like an empty backlog.
              aria-label={
                activeFilters.length === 0
                  ? "Filters, none set"
                  : `Filters, ${activeFilters.length} set: ${activeFilters.join(", ")}`
              }
              title="Filters"
              onClick={() => setFiltersOpen((open) => !open)}
              className={`touch-target flex min-w-[44px] items-center justify-center gap-1.5 rounded-lg border px-2.5 text-sm ${
                activeFilters.length > 0
                  ? "border-blue-500 bg-blue-950/40 text-blue-200"
                  : "border-dark-border bg-dark-bg text-dark-text hover:bg-dark-border"
              }`}
            >
              {/* An icon rather than the word (task-385): the sidebar is 320px, and the
                  word cost a third of the search box. The name above still says it. */}
              <svg width="20" height="20" viewBox="0 0 20 20" aria-hidden="true" focusable="false" data-testid="filter-icon">
                <g stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" fill="none">
                  <path d="M2.5 5h7.5M14 5h3.5M2.5 10h2.5M9 10h8.5M2.5 15h8.5M15 15h2.5" />
                  <circle cx="12" cy="5" r="2" />
                  <circle cx="7" cy="10" r="2" />
                  <circle cx="13" cy="15" r="2" />
                </g>
              </svg>
              {activeFilters.length > 0 && (
                <span
                  data-testid="active-filter-count"
                  aria-hidden="true"
                  className="rounded-full bg-blue-600 px-1.5 text-xs font-semibold text-white"
                >
                  {activeFilters.length}
                </span>
              )}
            </button>
            {filtersOpen && (
              // Right-aligned and width-capped against the viewport, which is what
              // keeps it on screen in a 320px sidebar and on a phone alike. Below the
              // header's `z-30` on purpose: a control inside the scrolling list must
              // not paint over the pinned bar above it.
              <div
                id={FILTER_POPOVER_ID}
                role="dialog"
                aria-label="Filters"
                className="absolute right-0 top-full z-20 mt-2 w-64 max-w-[calc(100vw-2rem)] space-y-3 rounded-lg border border-dark-border bg-dark-surface p-4 shadow-xl"
              >
                <label className="sr-only" htmlFor="status-filter">Status</label>
                <select ref={firstFilterRef} id="status-filter" aria-label="Status" value={status} onChange={(event) => updateParam("status", event.target.value, "open")} className="touch-target w-full rounded-lg border border-dark-border bg-dark-bg px-3">
                  <option value="open">Open (not closed)</option><option value="all">All Status</option><option value="attention">Waiting on you</option><option value="draft">Draft</option><option value="ready">Ready</option><option value="active">Working</option><option value="finishing">{STATUSES.finishing?.label}</option><option value="human">Needs Human</option><option value="external">Blocked</option><option value="reset">Quota reset</option><option value="closed">Closed</option>
                </select>
                <label className="sr-only" htmlFor="priority-filter">Priority</label>
                <select id="priority-filter" aria-label="Priority" value={priority} onChange={(event) => updateParam("priority", event.target.value, "all")} className="touch-target w-full rounded-lg border border-dark-border bg-dark-bg px-3">
                  <option value="all">All Priorities</option><option value="critical">Critical</option><option value="high">High</option><option value="medium">Medium</option><option value="low">Low</option>
                </select>
                <label className="sr-only" htmlFor="scope-filter">Scope</label>
                <select id="scope-filter" aria-label="Scope" value={scope} onChange={(event) => updateParam("scope", event.target.value, "all")} className="touch-target w-full rounded-lg border border-dark-border bg-dark-bg px-3">
                  <option value="all">All Tasks</option><option value="project">Project Tasks</option><option value="test">Test/Examples</option>
                </select>
                <button
                  type="button"
                  onClick={clearFilters}
                  disabled={!anyFilterSet}
                  className="touch-target w-full rounded-lg border border-dark-border px-3 text-sm text-dark-text hover:bg-dark-border disabled:opacity-50"
                >
                  Clear all filters
                </button>
              </div>
            )}
          </div>
          {/* The keyboard help, behind a `?` rather than on screen (task-385). It was
              two lines above the first row of a 320px sidebar, read once and then paid
              for on every visit. Hover shows it as a tooltip; a click or a tap pins it,
              which is the only way in on a touch screen and from the keyboard. */}
          {(tree || handlers) && (
            <div
              className="relative shrink-0"
              ref={helpRef}
              onPointerEnter={(event) => {
                if (event.pointerType === "mouse") setHelpPeek(true);
              }}
              onPointerLeave={(event) => {
                if (event.pointerType === "mouse") setHelpPeek(false);
              }}
            >
              <button
                type="button"
                id={HELP_BUTTON_ID}
                aria-label="Keyboard shortcuts"
                aria-expanded={helpShown}
                aria-controls={REORDER_HELP_ID}
                onClick={() => {
                  // Closing clears the peek too, so a click closes it under a mouse
                  // that is still resting on the button.
                  if (helpPinned) {
                    setHelpPinned(false);
                    setHelpPeek(false);
                  } else {
                    setHelpPinned(true);
                  }
                }}
                className="touch-target flex min-w-[44px] items-center justify-center rounded-lg border border-dark-border bg-dark-bg px-2.5 text-sm font-semibold text-dark-muted hover:bg-dark-border hover:text-dark-text"
              >
                <span aria-hidden="true">?</span>
              </button>
              <div
                id={REORDER_HELP_ID}
                data-testid="keyboard-help"
                data-open={helpShown}
                className={
                  helpShown
                    ? "absolute right-0 top-full z-20 mt-2 w-72 max-w-[calc(100vw-2rem)] rounded-lg border border-dark-border bg-dark-surface p-3 text-xs leading-5 text-dark-muted shadow-xl"
                    : "sr-only"
                }
              >
                {tree ? (
                  <>
                    <kbd>↑</kbd><kbd>↓</kbd> select · <kbd>←</kbd><kbd>→</kbd> fold,
                    remembered for this project
                    {handlers && (
                      <>
                        {" · "}
                        <kbd>Alt</kbd>+<kbd>↑</kbd><kbd>↓</kbd> step a task through its priority
                        band, <kbd>Alt</kbd>+<kbd>Home</kbd>/<kbd>End</kbd> for the ends. Dragging a grip
                        does the same thing.
                      </>
                    )}
                  </>
                ) : (
                  <>
                    Rows are in queue order. Focus a task and press <kbd>Alt</kbd>+<kbd>↑</kbd> or{" "}
                    <kbd>Alt</kbd>+<kbd>↓</kbd> to step it through its priority band, or{" "}
                    <kbd>Alt</kbd>+<kbd>Home</kbd> and <kbd>Alt</kbd>+<kbd>End</kbd> for the ends.
                    Dragging a grip does the same thing.
                  </>
                )}
              </div>
            </div>
          )}
        </div>
        {!handlers && unavailableReason && (
          <p className="text-xs text-dark-muted">{unavailableReason}</p>
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
