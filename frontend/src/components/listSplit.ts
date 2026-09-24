/**
 * Where the boundary between the task list and the record sits, and how far it may move
 * (task-368).
 *
 * Everything here is arithmetic on one number -- the width of the grid the two regions
 * share -- so it is testable without a browser. `ListDetailSplit` measures that width
 * and renders the answer; nothing else in the app needs to know the divider exists.
 */

/**
 * The list column's default width: a share of the grid, capped.
 *
 * **This is today's ratio, unchanged, and the one place it lives.** It was the class
 * string `grid-cols-[minmax(20rem,min(34%,36rem))_minmax(0,1fr)]` from task-237 until
 * task-368: a third of the grid, so a 2560px monitor gets a readable list rather than a
 * fixed column with the margin this epic exists to reclaim, capped at 36rem where more
 * width stopped helping. The owner said on 2026-09-06 that this ratio is good and that
 * he may later want his own dragged width to become the default. That is a one-line
 * change here: a fixed width of N px is `{ share: 1, maxPx: N }`.
 */
export const DEFAULT_LIST_SPLIT = { share: 0.34, maxPx: 576 } as const;

/**
 * The narrowest the list may be, from task-235's 320-400px column: 20rem is what a task
 * row needs to stay readable, and task-356 moved the filters behind a button to buy the
 * column exactly this width. It was already the floor of the old `minmax()`.
 */
export const LIST_MIN_PX = 320;

/**
 * The content width at which the record restacks: `@min-[768px]:` throughout
 * `TaskDetail`, measured by task-239. Below it the metadata strip halves, the header
 * stacks and the relationship cards go to one column -- a real behaviour change, which
 * is why dragging must not push the record there by accident.
 */
export const DETAIL_FIT_PX = 768;

/** The divider's own column. The old grid's `gap-6`, so the default layout is unmoved. */
export const DIVIDER_PX = 24;

/** One arrow keypress. Shift moves four of them. */
export const KEY_STEP_PX = 16;

/**
 * Where this browser keeps the width. Following task-238's `agentjobs.tasks.*`
 * convention (`agentjobs.tasks.collapsed.<project>`), but **not per project**: a fold
 * names task ids and belongs to one backlog, while this is a property of the screen the
 * reader is sitting at.
 */
export const LIST_WIDTH_KEY = "agentjobs.tasks.listWidth";

/**
 * The same default as a grid track, for the first paint before the grid has been
 * measured -- and for jsdom, which never measures it. It is the old class string's
 * track, generated from the constant so the two cannot disagree.
 */
export const DEFAULT_LIST_TRACK = `minmax(${LIST_MIN_PX}px,min(${
  DEFAULT_LIST_SPLIT.share * 100
}%,${DEFAULT_LIST_SPLIT.maxPx}px))`;

/** The list's width with nothing stored: exactly what the old `minmax()` resolved to. */
export function defaultListWidth(gridWidth: number): number {
  const { share, maxPx } = DEFAULT_LIST_SPLIT;
  return Math.max(LIST_MIN_PX, Math.min(share * gridWidth, maxPx));
}

/**
 * How far the list may be dragged at this grid width.
 *
 * The record's floor is 768 **or whatever the default already gives it, if that is
 * less**. At a 1024px tablet the grid is 960 and 320 + 24 + 768 does not fit, so a flat
 * 768 floor cannot hold there; the default layout already restacks the record at that
 * width, and the rule the divider keeps is that it never causes a restack the default
 * did not. So on a grid narrower than 1200px (a 1264px window) the record cannot be
 * narrowed at all, and the list can only be made narrower, down to its own floor.
 */
export function listBounds(gridWidth: number): { min: number; max: number } {
  const room = gridWidth - DIVIDER_PX;
  const detailFloor = Math.min(DETAIL_FIT_PX, room - defaultListWidth(gridWidth));
  // Whole pixels, rounded down, so the ceiling never gives the record back less than
  // its floor by a fraction and the separator's aria-valuemax is a number people read.
  return { min: LIST_MIN_PX, max: Math.max(LIST_MIN_PX, Math.floor(room - detailFloor)) };
}

export function clampListWidth(width: number, gridWidth: number): number {
  const { min, max } = listBounds(gridWidth);
  return Math.round(Math.min(max, Math.max(min, width)));
}

/**
 * The stored width, or null for "use the default".
 *
 * Anything unreadable is a miss rather than an error, as for the folds: a layout
 * preference is a convenience, and losing one must never cost a reader the screen.
 */
export function readListWidth(): number | null {
  try {
    const raw = window.localStorage.getItem(LIST_WIDTH_KEY);
    if (raw === null) return null;
    const value = Number(raw);
    return Number.isFinite(value) && value > 0 ? value : null;
  } catch {
    return null;
  }
}

/** Null forgets the setting, which returns the reader to the default ratio. */
export function writeListWidth(width: number | null): void {
  try {
    if (width === null) window.localStorage.removeItem(LIST_WIDTH_KEY);
    else window.localStorage.setItem(LIST_WIDTH_KEY, String(Math.round(width)));
  } catch {
    // Storage full, or disabled. The width still holds for this session.
  }
}
