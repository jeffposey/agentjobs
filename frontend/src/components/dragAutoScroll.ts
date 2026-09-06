/**
 * Scroll the list while a drag is held near the top or bottom of its scrollport.
 *
 * A browser does not do this for you. Chrome autoscrolls a scrollable *element* the
 * drag is inside, and does nothing for the document itself, so a list taller than the
 * viewport can only be dropped on rows that were already on screen when the gesture
 * started. task-207 shipped drag as the accelerator for reordering the backlog and
 * that made the common move -- pull a task from the bottom of the list to the top of
 * its band -- the one move drag could not perform.
 *
 * **The scrollport is not always the window** (task-237). On the two-region Tasks
 * surface the list has a scroll container of its own and the page does not scroll at
 * all, so a loop hard-wired to `window.scrollBy` would move nothing and the feature
 * would be gone on exactly the screens it was built for. `within` names an element
 * inside the list; the loop finds the scrollable box around it and drives that,
 * falling back to the window when there is none -- which is the stacked phone shell,
 * where the page is still the scroller.
 *
 * The loop is deliberately independent of how often `dragover` fires. It records the
 * pointer's Y whenever an event arrives and keeps scrolling from that reading until a
 * newer one replaces it, so holding the pointer perfectly still at the edge -- which
 * is what a person actually does -- scrolls at full rate whether or not the browser
 * bothers to keep firing events at a stationary pointer.
 */

/** How close to an edge the pointer has to be before the page moves, in CSS pixels. */
export const EDGE_ZONE_PX = 80;
/** Rate at the outer boundary of the zone: slow enough to place a row precisely. */
export const MIN_SPEED_PX_PER_SEC = 90;
/** Rate at the very edge: fast enough to cross several screens without waiting. */
export const MAX_SPEED_PX_PER_SEC = 1100;
/**
 * The largest gap between frames the loop will act on. A tab that was throttled or a
 * frame the machine simply missed would otherwise be paid back all at once, which
 * reads as the page teleporting rather than scrolling.
 */
const MAX_FRAME_MS = 100;

/**
 * Pixels per second the page should move for a pointer at `clientY`, negative up.
 *
 * Zero outside the edge zones. Inside one, the rate ramps with proximity on a squared
 * curve: gentle where the zone begins, so a row can be nudged one line at a time, and
 * steep at the last few pixels, where the intent is unambiguously "keep going".
 */
export function edgeScrollVelocity(clientY: number, viewportHeight: number): number {
  if (viewportHeight <= 0) return 0;
  // A zone taller than half the window would leave no neutral band in the middle, so
  // on a short window both zones shrink rather than meeting.
  const zone = Math.min(EDGE_ZONE_PX, viewportHeight / 2);
  if (zone <= 0) return 0;

  const rate = (distance: number) => {
    const ratio = Math.min(Math.max((zone - distance) / zone, 0), 1);
    return MIN_SPEED_PX_PER_SEC + (MAX_SPEED_PX_PER_SEC - MIN_SPEED_PX_PER_SEC) * ratio * ratio;
  };

  if (clientY < zone) return -rate(Math.max(clientY, 0));
  if (clientY > viewportHeight - zone) return rate(Math.max(viewportHeight - clientY, 0));
  return 0;
}

export type DragAutoScrollDeps = {
  /** Where the drag events are listened for. Defaults to the document. */
  events?: Pick<Document, "addEventListener" | "removeEventListener">;
  /**
   * An element inside the list being dragged. The nearest scrollable box around it is
   * what the loop moves; with none, or with nothing passed, it moves the window.
   */
  within?: Element | null;
  scrollBy?: (dy: number) => void;
  /** Where the scrollport starts in viewport coordinates. Zero for the window. */
  viewportTop?: () => number;
  viewportHeight?: () => number;
  requestFrame?: (callback: (time: number) => void) => number;
  cancelFrame?: (handle: number) => void;
};

/**
 * The scrollable box `element` sits in, or `null` for "the window".
 *
 * Both halves of the test are needed. An ancestor whose `overflow-y` is `auto` but
 * whose content fits is not scrolling anything, and driving it would silently do
 * nothing; an ancestor taller than its box but with `overflow: visible` is not a
 * scrollport at all, it just overflows.
 */
export function nearestScroller(element: Element | null | undefined): Element | null {
  if (typeof window === "undefined" || typeof window.getComputedStyle !== "function") return null;
  let candidate = element?.parentElement ?? null;
  while (candidate && candidate !== document.body && candidate !== document.documentElement) {
    const overflow = window.getComputedStyle(candidate).overflowY;
    if (
      (overflow === "auto" || overflow === "scroll") &&
      candidate.scrollHeight > candidate.clientHeight
    ) {
      return candidate;
    }
    candidate = candidate.parentElement;
  }
  return null;
}

/**
 * Start the loop. Returns the teardown, which is idempotent.
 *
 * The caller starts this when its own drag begins, so a drag that started somewhere
 * else -- a link, a file dragged in from the desktop -- never scrolls the page. The
 * loop also tears itself down on `drop` and `dragend`, because a rAF loop that
 * outlives its gesture would scroll the page under someone who is no longer dragging,
 * and that must not depend on a caller's state being unwound correctly.
 */
export function startDragAutoScroll(deps: DragAutoScrollDeps = {}): () => void {
  const events = deps.events ?? document;
  // Resolved once, at the start of the gesture: the box is not going to change under a
  // drag, and re-walking the ancestors on every frame would cost a layout read a frame.
  const scroller = nearestScroller(deps.within);
  const scrollBy =
    deps.scrollBy ??
    (scroller
      ? (dy: number) => {
          scroller.scrollTop += dy;
        }
      : (dy: number) => window.scrollBy(0, dy));
  const viewportTop =
    deps.viewportTop ?? (scroller ? () => scroller.getBoundingClientRect().top : () => 0);
  const viewportHeight =
    deps.viewportHeight ?? (scroller ? () => scroller.clientHeight : () => window.innerHeight);
  const requestFrame =
    deps.requestFrame ?? ((callback: (time: number) => void) => window.requestAnimationFrame(callback));
  const cancelFrame = deps.cancelFrame ?? ((handle: number) => window.cancelAnimationFrame(handle));

  let pointerY: number | null = null;
  let frame: number | null = null;
  let previous: number | null = null;
  let stopped = false;

  const step = (time: number) => {
    if (stopped) return;
    frame = requestFrame(step);
    const elapsed = previous === null ? 0 : Math.min(time - previous, MAX_FRAME_MS);
    previous = time;
    if (pointerY === null || elapsed <= 0) return;
    // `clientY` is viewport-relative and the zones are scrollport-relative, so a
    // scroller that does not start at the top of the window has to be subtracted out.
    const velocity = edgeScrollVelocity(pointerY - viewportTop(), viewportHeight());
    if (velocity !== 0) scrollBy((velocity * elapsed) / 1000);
  };

  const onDragOver = (event: Event) => {
    // `clientY` is what the browser reports for the pointer during a drag; there is no
    // pointermove to read while an HTML5 drag is in flight.
    pointerY = (event as DragEvent).clientY;
  };

  const stop = () => {
    if (stopped) return;
    stopped = true;
    if (frame !== null) cancelFrame(frame);
    frame = null;
    events.removeEventListener("dragover", onDragOver, true);
    events.removeEventListener("drop", stop, true);
    events.removeEventListener("dragend", stop, true);
  };

  events.addEventListener("dragover", onDragOver, true);
  events.addEventListener("drop", stop, true);
  events.addEventListener("dragend", stop, true);
  frame = requestFrame(step);
  return stop;
}
