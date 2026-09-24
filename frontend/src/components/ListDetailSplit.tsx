import { useId, useLayoutEffect, useRef, useState, type ReactNode } from "react";

import {
  DEFAULT_LIST_TRACK,
  DIVIDER_PX,
  KEY_STEP_PX,
  clampListWidth,
  defaultListWidth,
  listBounds,
  readListWidth,
  writeListWidth,
} from "./listSplit";

/** A drag in progress. The width lives here as well as in state, so `end` reads it fresh. */
interface Drag {
  pointerId: number;
  startX: number;
  startWidth: number;
  width: number;
}

/**
 * The two regions of the Tasks surface and the divider between them (task-368).
 *
 * Rendered only in the wide shell -- `TasksSurface` decides that with `useWideShell()`,
 * so there is one answer to "is this the wide shell" and a phone never meets a divider.
 *
 * **Pointer events on the divider alone, and that is how it coexists with reordering.**
 * The sidebar's rows reorder with native HTML drag-and-drop (task-207), which starts
 * from a `dragstart` on a row's own handle and, once started, suppresses pointer events
 * for its duration. The divider is a sibling of the list, not inside it, is not
 * `draggable`, and takes no `dragover`, so a row can neither start a resize nor be
 * dropped on the divider; and a resize captures its pointer to the divider, so a finger
 * that wanders over a row mid-resize is still resizing. `touch-none` stops a tablet
 * from turning the same finger into a scroll.
 *
 * The width is committed -- clamped, then written to `localStorage` -- when a gesture
 * ends, not on every move; a cancelled pointer reverts rather than commits. What is
 * rendered is always re-clamped to the current grid, but a narrower window never
 * overwrites what was stored, so widening it again gives the reader their width back.
 */
export function ListDetailSplit({ list, detail }: { list: ReactNode; detail: ReactNode }) {
  const listId = useId();
  const gridRef = useRef<HTMLDivElement>(null);
  const drag = useRef<Drag | null>(null);
  const [gridWidth, setGridWidth] = useState(0);
  const [stored, setStored] = useState<number | null>(() => readListWidth());
  const [dragWidth, setDragWidth] = useState<number | null>(null);

  // A layout effect so the measured width is in place before the first paint; the
  // default track below covers the render before it, and gives the same answer.
  useLayoutEffect(() => {
    const grid = gridRef.current;
    if (!grid) return;
    const measure = () => setGridWidth(grid.getBoundingClientRect().width);
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(grid);
    return () => observer.disconnect();
  }, []);

  const measured = gridWidth > 0;
  const bounds = measured ? listBounds(gridWidth) : null;
  const width = measured
    ? clampListWidth(dragWidth ?? stored ?? defaultListWidth(gridWidth), gridWidth)
    : null;
  const track = width === null ? DEFAULT_LIST_TRACK : `${width}px`;

  const commit = (next: number | null) => {
    const value = next === null ? null : clampListWidth(next, gridWidth);
    setStored(value);
    writeListWidth(value);
  };

  const end = (keep: boolean) => {
    const current = drag.current;
    if (!current) return;
    drag.current = null;
    setDragWidth(null);
    if (keep && current.width !== current.startWidth) commit(current.width);
  };

  const onKeyDown = (event: React.KeyboardEvent) => {
    if (width === null || !bounds) return;
    const step = event.shiftKey ? KEY_STEP_PX * 4 : KEY_STEP_PX;
    let next: number | null = null;
    if (event.key === "ArrowLeft") next = width - step;
    else if (event.key === "ArrowRight") next = width + step;
    else if (event.key === "Home") next = bounds.min;
    else if (event.key === "End") next = bounds.max;
    else return;
    event.preventDefault();
    commit(next);
  };

  return (
    <div
      ref={gridRef}
      className={`grid min-h-0 flex-1 ${dragWidth === null ? "" : "cursor-col-resize select-none"}`}
      style={{ gridTemplateColumns: `${track} ${DIVIDER_PX}px minmax(0,1fr)` }}
      data-split={stored === null ? "default" : "stored"}
    >
      <aside id={listId} aria-label="Task list" data-region="list" className="min-h-0 overflow-y-auto pr-1">
        {list}
      </aside>
      {/* Focusable and arrow-key operable because a divider only a mouse can move is
          not a divider for anybody without one. Double-click returns to the default. */}
      <div
        role="separator"
        aria-orientation="vertical"
        aria-label="Resize the task list"
        aria-controls={listId}
        aria-valuenow={width ?? undefined}
        aria-valuemin={bounds?.min}
        aria-valuemax={bounds?.max}
        aria-valuetext={width === null ? undefined : `Task list ${width} pixels wide`}
        tabIndex={0}
        data-divider=""
        className="group relative cursor-col-resize touch-none select-none focus:outline-none"
        onKeyDown={onKeyDown}
        onDoubleClick={() => commit(null)}
        onPointerDown={(event) => {
          if (width === null || event.button !== 0) return;
          // No text selection, no native drag, and no competing gesture from this press.
          event.preventDefault();
          event.currentTarget.focus();
          event.currentTarget.setPointerCapture?.(event.pointerId);
          drag.current = { pointerId: event.pointerId, startX: event.clientX, startWidth: width, width };
          setDragWidth(width);
        }}
        onPointerMove={(event) => {
          const current = drag.current;
          if (!current || event.pointerId !== current.pointerId) return;
          current.width = clampListWidth(current.startWidth + event.clientX - current.startX, gridWidth);
          setDragWidth(current.width);
        }}
        onPointerUp={(event) => {
          if (drag.current?.pointerId === event.pointerId) end(true);
        }}
        onPointerCancel={(event) => {
          if (drag.current?.pointerId === event.pointerId) end(false);
        }}
        onLostPointerCapture={() => end(true)}
      >
        <span
          aria-hidden="true"
          className={`absolute inset-y-0 left-1/2 w-px -translate-x-1/2 ${
            dragWidth === null ? "bg-transparent group-hover:bg-dark-border" : "bg-blue-400"
          } group-focus-visible:bg-blue-400`}
        />
        <span
          aria-hidden="true"
          className="absolute left-1/2 top-1/2 h-8 w-1 -translate-x-1/2 -translate-y-1/2 rounded-full bg-dark-border group-hover:bg-dark-muted group-focus-visible:bg-blue-400"
        />
      </div>
      <section aria-label="Task detail" data-region="detail" className="min-h-0 overflow-y-auto">
        {detail}
      </section>
    </div>
  );
}
