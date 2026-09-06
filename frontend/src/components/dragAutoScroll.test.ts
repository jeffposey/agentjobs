import { describe, expect, it, vi } from "vitest";

import {
  EDGE_ZONE_PX,
  MAX_SPEED_PX_PER_SEC,
  MIN_SPEED_PX_PER_SEC,
  edgeScrollVelocity,
  nearestScroller,
  startDragAutoScroll,
} from "./dragAutoScroll";

const VIEWPORT = 800;

/**
 * A hand-driven frame clock and a fake document.
 *
 * The loop is driven a frame at a time rather than by real `requestAnimationFrame`,
 * because what is being asserted is how far the page moves for a given pointer and a
 * given amount of elapsed time -- and a test that waited on the machine's frame rate
 * could assert neither of those.
 */
function harness() {
  const listeners = new Map<string, Set<EventListener>>();
  const scrolled: Array<number> = [];
  let pending: ((time: number) => void) | null = null;
  let cancelled = 0;
  let now = 0;

  const events = {
    addEventListener: (type: string, listener: EventListener) => {
      listeners.set(type, (listeners.get(type) ?? new Set()).add(listener));
    },
    removeEventListener: (type: string, listener: EventListener) => {
      listeners.get(type)?.delete(listener);
    },
  } as unknown as Document;

  const stop = startDragAutoScroll({
    events,
    scrollBy: (dy) => scrolled.push(dy),
    viewportHeight: () => VIEWPORT,
    requestFrame: (callback) => {
      pending = callback;
      return 1;
    },
    cancelFrame: () => {
      cancelled += 1;
      pending = null;
    },
  });

  return {
    scrolled,
    stop,
    get cancelled() {
      return cancelled;
    },
    listenerCount: () => [...listeners.values()].reduce((total, set) => total + set.size, 0),
    fire: (type: string, event: Partial<DragEvent>) => {
      for (const listener of listeners.get(type) ?? []) listener(event as Event);
    },
    /** Run one frame, `ms` after the previous one. */
    frame: (ms = 16) => {
      now += ms;
      const callback = pending;
      pending = null;
      callback?.(now);
    },
    scrolledTotal: () => scrolled.reduce((total, dy) => total + dy, 0),
  };
}

describe("edgeScrollVelocity", () => {
  it("is still in the middle of the window", () => {
    expect(edgeScrollVelocity(VIEWPORT / 2, VIEWPORT)).toBe(0);
    expect(edgeScrollVelocity(EDGE_ZONE_PX + 1, VIEWPORT)).toBe(0);
    expect(edgeScrollVelocity(VIEWPORT - EDGE_ZONE_PX - 1, VIEWPORT)).toBe(0);
  });

  it("scrolls up near the top and down near the bottom", () => {
    expect(edgeScrollVelocity(EDGE_ZONE_PX - 1, VIEWPORT)).toBeLessThan(0);
    expect(edgeScrollVelocity(VIEWPORT - EDGE_ZONE_PX + 1, VIEWPORT)).toBeGreaterThan(0);
  });

  it("ramps with proximity, and is symmetric", () => {
    const outer = -edgeScrollVelocity(EDGE_ZONE_PX - 1, VIEWPORT);
    const middle = -edgeScrollVelocity(EDGE_ZONE_PX / 2, VIEWPORT);
    const inner = -edgeScrollVelocity(0, VIEWPORT);
    expect(outer).toBeLessThan(middle);
    expect(middle).toBeLessThan(inner);
    expect(outer).toBeCloseTo(MIN_SPEED_PX_PER_SEC, 0);
    expect(inner).toBeCloseTo(MAX_SPEED_PX_PER_SEC, 5);
    expect(edgeScrollVelocity(VIEWPORT - EDGE_ZONE_PX / 2, VIEWPORT)).toBeCloseTo(middle, 5);
  });

  it("clamps a pointer the browser reports outside the window", () => {
    expect(edgeScrollVelocity(-40, VIEWPORT)).toBeCloseTo(-MAX_SPEED_PX_PER_SEC, 5);
    expect(edgeScrollVelocity(VIEWPORT + 40, VIEWPORT)).toBeCloseTo(MAX_SPEED_PX_PER_SEC, 5);
  });

  it("leaves a neutral band on a window too short for two full zones", () => {
    // 100px tall: the zones shrink to 50 each rather than overlapping, so the exact
    // middle is still a place where nothing happens.
    expect(edgeScrollVelocity(50, 100)).toBe(0);
    expect(edgeScrollVelocity(0, 100)).toBeLessThan(0);
    expect(edgeScrollVelocity(100, 100)).toBeGreaterThan(0);
    expect(edgeScrollVelocity(10, 0)).toBe(0);
  });
});

describe("startDragAutoScroll", () => {
  it("does nothing until a drag reports where the pointer is", () => {
    const loop = harness();
    loop.frame();
    loop.frame();
    expect(loop.scrolled).toEqual([]);
    loop.stop();
  });

  it("does nothing while the pointer is away from the edges", () => {
    const loop = harness();
    loop.fire("dragover", { clientY: VIEWPORT / 2 });
    loop.frame();
    loop.frame();
    expect(loop.scrolled).toEqual([]);
    loop.stop();
  });

  it("scrolls by the elapsed time, not by the frame count", () => {
    const loop = harness();
    loop.fire("dragover", { clientY: VIEWPORT });
    loop.frame();
    loop.frame(16);
    const oneFrame = loop.scrolledTotal();
    loop.frame(32);
    // Twice the time, twice the distance -- so a slow machine covers the same ground.
    expect(loop.scrolledTotal() - oneFrame).toBeCloseTo(oneFrame * 2, 5);
    expect(oneFrame).toBeCloseTo((MAX_SPEED_PX_PER_SEC * 16) / 1000, 5);
    loop.stop();
  });

  it("keeps scrolling while the pointer is held perfectly still", () => {
    const loop = harness();
    // One dragover and then nothing, which is what a held hand produces.
    loop.fire("dragover", { clientY: 2 });
    loop.frame();
    for (let i = 0; i < 10; i += 1) loop.frame(16);
    expect(loop.scrolled).toHaveLength(10);
    expect(loop.scrolledTotal()).toBeLessThan(0);
    loop.stop();
  });

  it("reverses when the pointer crosses to the other edge", () => {
    const loop = harness();
    loop.fire("dragover", { clientY: VIEWPORT - 2 });
    loop.frame();
    loop.frame(16);
    expect(loop.scrolledTotal()).toBeGreaterThan(0);
    loop.fire("dragover", { clientY: 2 });
    loop.frame(16);
    expect(loop.scrolled.at(-1)).toBeLessThan(0);
    loop.stop();
  });

  it("does not repay a frame the machine missed all at once", () => {
    const loop = harness();
    loop.fire("dragover", { clientY: VIEWPORT });
    loop.frame();
    loop.frame(5000);
    // Capped at 100ms of movement rather than five seconds of it.
    expect(loop.scrolled[0]).toBeCloseTo((MAX_SPEED_PX_PER_SEC * 100) / 1000, 5);
    loop.stop();
  });

  it("tears itself down on a drop, and cannot be revived by a stale frame", () => {
    const loop = harness();
    loop.fire("dragover", { clientY: VIEWPORT });
    loop.frame();
    loop.fire("drop", {});
    expect(loop.listenerCount()).toBe(0);
    expect(loop.cancelled).toBe(1);
    loop.frame(16);
    expect(loop.scrolled).toEqual([]);
  });

  it("tears itself down on dragend, which is also the cancelled-drag path", () => {
    // Escape during a drag, or a drop on something that is not a target, both end in
    // `dragend` and nothing else -- so this is the case the loop must not survive.
    const loop = harness();
    loop.fire("dragover", { clientY: 0 });
    loop.frame();
    loop.fire("dragend", {});
    expect(loop.listenerCount()).toBe(0);
    loop.frame(16);
    expect(loop.scrolled).toEqual([]);
  });

  it("is idempotent, so a caller unwinding after dragend is harmless", () => {
    const loop = harness();
    loop.fire("dragend", {});
    loop.stop();
    loop.stop();
    expect(loop.cancelled).toBe(1);
  });

  it("defaults to the real document and window", () => {
    let dy: number | null = null;
    vi.spyOn(window, "scrollBy").mockImplementation(((_x: number, y: number) => {
      dy = y;
    }) as typeof window.scrollBy);
    const frames: Array<(time: number) => void> = [];
    vi.spyOn(window, "requestAnimationFrame").mockImplementation((callback) =>
      frames.push(callback),
    );
    const cancel = vi.spyOn(window, "cancelAnimationFrame").mockImplementation(() => {});
    try {
      const stop = startDragAutoScroll();
      document.dispatchEvent(new MouseEvent("dragover", { clientY: window.innerHeight }));
      frames.shift()?.(0);
      frames.shift()?.(16);
      expect(dy).toBeGreaterThan(0);
      stop();
      expect(cancel).toHaveBeenCalled();
    } finally {
      vi.restoreAllMocks();
    }
  });
});

/**
 * task-237 put the list in a region of its own, and the page under it stopped
 * scrolling. A loop wired to `window.scrollBy` would then move nothing at all on the
 * one shell the feature exists for, and would do it silently.
 *
 * jsdom lays nothing out, so `scrollHeight`/`clientHeight` and the box are stubbed:
 * what is under test is which element the loop picks and where it puts the edge zones,
 * not whether a browser overflows. The browser half is `e2e/tasks-shell.spec.ts`.
 */
describe("the box the loop scrolls", () => {
  function scrollport(options: { overflowY: string; scrollHeight: number; top?: number }) {
    const element = document.createElement("div");
    const inner = document.createElement("div");
    element.appendChild(inner);
    document.body.appendChild(element);
    element.style.overflowY = options.overflowY;
    Object.defineProperty(element, "scrollHeight", { value: options.scrollHeight });
    Object.defineProperty(element, "clientHeight", { value: 400 });
    element.getBoundingClientRect = () =>
      ({ top: options.top ?? 100, bottom: (options.top ?? 100) + 400, height: 400 }) as DOMRect;
    return { element, inner };
  }

  it("finds the scrollable ancestor the dragged row is inside", () => {
    const { element, inner } = scrollport({ overflowY: "auto", scrollHeight: 2000 });
    expect(nearestScroller(inner)).toBe(element);
  });

  it("ignores an ancestor whose content already fits, and one that is not a scrollport", () => {
    expect(nearestScroller(scrollport({ overflowY: "auto", scrollHeight: 400 }).inner)).toBeNull();
    expect(
      nearestScroller(scrollport({ overflowY: "visible", scrollHeight: 2000 }).inner),
    ).toBeNull();
  });

  it("scrolls that box, with the edge zones measured from the box rather than the window", () => {
    const { element, inner } = scrollport({ overflowY: "auto", scrollHeight: 2000, top: 100 });
    const frames: Array<(time: number) => void> = [];
    const requestFrame = (callback: (time: number) => void) => frames.push(callback);
    const stop = startDragAutoScroll({
      within: inner,
      requestFrame,
      cancelFrame: () => {},
    });

    // 260px down the window is 160px into a 400px box: the middle of it, and nowhere
    // near an edge. A loop still measuring against an 768px-tall jsdom window would
    // read it as 500px from the bottom and also do nothing, so the next case is the
    // one that separates them.
    document.dispatchEvent(new MouseEvent("dragover", { clientY: 260 }));
    frames.shift()?.(0);
    frames.shift()?.(16);
    expect(element.scrollTop).toBe(0);

    // 495px down the window is 5px from the bottom of the box, and 273px from the
    // bottom of the window -- so this moves only if the box is what is being measured.
    document.dispatchEvent(new MouseEvent("dragover", { clientY: 495 }));
    frames.shift()?.(32);
    frames.shift()?.(48);
    expect(element.scrollTop).toBeGreaterThan(0);
    stop();
  });
});
