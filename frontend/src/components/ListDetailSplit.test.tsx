import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ListDetailSplit } from "./ListDetailSplit";
import {
  DEFAULT_LIST_TRACK,
  LIST_WIDTH_KEY,
  clampListWidth,
  defaultListWidth,
  listBounds,
} from "./listSplit";

/**
 * task-368: the divider between the list and the record.
 *
 * jsdom does not lay out, so each test gives the grid a width by stubbing
 * `getBoundingClientRect` -- the one number `ListDetailSplit` measures -- and then
 * asserts the values it renders: the grid track it writes and the separator's aria
 * values. What a browser does with that track is `e2e/list-divider.spec.ts`.
 */

/** The grid at a 1280x800 window: 1280 less `lg:px-8` on each side. */
const GRID_1280 = 1216;
/** The grid at a 1920 window. */
const GRID_1920 = 1856;

function withGridWidth(width: number) {
  return vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({
    width,
    height: 700,
    x: 0,
    y: 0,
    top: 0,
    left: 0,
    right: width,
    bottom: 700,
    toJSON: () => ({}),
  } as DOMRect);
}

function renderSplit() {
  render(<ListDetailSplit list={<p>list</p>} detail={<p>detail</p>} />);
  const separator = screen.getByRole("separator", { name: "Resize the task list" });
  const grid = separator.parentElement as HTMLElement;
  return { separator, grid, track: () => grid.style.gridTemplateColumns };
}

beforeEach(() => window.localStorage.clear());
afterEach(() => vi.restoreAllMocks());

describe("the bounds", () => {
  it("defaults to exactly what the old minmax() track resolved to", () => {
    expect(DEFAULT_LIST_TRACK).toBe("minmax(320px,min(34%,576px))");
    expect(defaultListWidth(GRID_1280)).toBeCloseTo(413.44);
    expect(defaultListWidth(GRID_1920)).toBe(576);
    expect(defaultListWidth(700)).toBe(320);
  });

  it("holds the record at 768 wherever the default gives it that much", () => {
    // 1216 - 24 - 768 = 424.
    expect(listBounds(GRID_1280)).toEqual({ min: 320, max: 424 });
    expect(listBounds(GRID_1920)).toEqual({ min: 320, max: 1856 - 24 - 768 });
  });

  it("never narrows the record past its default where the default is already under 768", () => {
    // A 1024 tablet: grid 960, default list 326.4, record 609.6 -- already restacked.
    expect(listBounds(960)).toEqual({ min: 320, max: 326 });
    // A portrait tablet: the list is at its floor and nothing moves.
    expect(listBounds(696)).toEqual({ min: 320, max: 320 });
  });

  it("clamps both ways", () => {
    expect(clampListWidth(50, GRID_1280)).toBe(320);
    expect(clampListWidth(5000, GRID_1280)).toBe(424);
    expect(clampListWidth(380, GRID_1280)).toBe(380);
  });
});

describe("the divider", () => {
  it("renders the default as a width with its real bounds", () => {
    withGridWidth(GRID_1280);
    const { separator, track, grid } = renderSplit();
    expect(separator.getAttribute("aria-valuenow")).toBe("413");
    expect(separator.getAttribute("aria-valuemin")).toBe("320");
    expect(separator.getAttribute("aria-valuemax")).toBe("424");
    expect(separator.getAttribute("aria-orientation")).toBe("vertical");
    expect(separator.tabIndex).toBe(0);
    expect(track()).toBe("413px 24px minmax(0,1fr)");
    expect(grid.dataset.split).toBe("default");
  });

  it("falls back to the default track when the grid has not been measured", () => {
    withGridWidth(0);
    const { separator, track } = renderSplit();
    expect(track()).toBe("minmax(320px,min(34%,576px)) 24px minmax(0,1fr)");
    expect(separator.hasAttribute("aria-valuenow")).toBe(false);
  });

  it("moves with the arrow keys, stops at its bounds, and stores the width", () => {
    withGridWidth(GRID_1280);
    const { separator, track } = renderSplit();
    fireEvent.keyDown(separator, { key: "ArrowLeft" });
    expect(separator.getAttribute("aria-valuenow")).toBe("397");
    expect(track()).toBe("397px 24px minmax(0,1fr)");
    expect(window.localStorage.getItem(LIST_WIDTH_KEY)).toBe("397");
    fireEvent.keyDown(separator, { key: "ArrowRight", shiftKey: true });
    expect(separator.getAttribute("aria-valuenow")).toBe("424");
    fireEvent.keyDown(separator, { key: "Home" });
    expect(separator.getAttribute("aria-valuenow")).toBe("320");
    fireEvent.keyDown(separator, { key: "ArrowLeft" });
    expect(separator.getAttribute("aria-valuenow")).toBe("320");
    fireEvent.keyDown(separator, { key: "End" });
    expect(window.localStorage.getItem(LIST_WIDTH_KEY)).toBe("424");
  });

  it("follows a pointer drag live and stores it only when the drag ends", () => {
    withGridWidth(GRID_1920);
    const { separator, track } = renderSplit();
    fireEvent.pointerDown(separator, { pointerId: 7, button: 0, clientX: 600 });
    fireEvent.pointerMove(separator, { pointerId: 7, clientX: 800 });
    expect(track()).toBe("776px 24px minmax(0,1fr)");
    expect(window.localStorage.getItem(LIST_WIDTH_KEY)).toBeNull();
    // Another pointer -- a second finger -- does not move it.
    fireEvent.pointerMove(separator, { pointerId: 9, clientX: 100 });
    expect(track()).toBe("776px 24px minmax(0,1fr)");
    fireEvent.pointerUp(separator, { pointerId: 7, clientX: 800 });
    expect(window.localStorage.getItem(LIST_WIDTH_KEY)).toBe("776");
  });

  it("reverts a cancelled drag rather than committing it", () => {
    withGridWidth(GRID_1920);
    const { separator, track } = renderSplit();
    fireEvent.pointerDown(separator, { pointerId: 3, button: 0, clientX: 600 });
    fireEvent.pointerMove(separator, { pointerId: 3, clientX: 400 });
    expect(track()).toBe("376px 24px minmax(0,1fr)");
    fireEvent.pointerCancel(separator, { pointerId: 3 });
    expect(track()).toBe("576px 24px minmax(0,1fr)");
    expect(window.localStorage.getItem(LIST_WIDTH_KEY)).toBeNull();
  });

  it("reads a stored width, clamps it to this grid without overwriting it, and resets on double-click", () => {
    window.localStorage.setItem(LIST_WIDTH_KEY, "900");
    withGridWidth(GRID_1280);
    const { separator, track, grid } = renderSplit();
    expect(grid.dataset.split).toBe("stored");
    expect(track()).toBe("424px 24px minmax(0,1fr)");
    expect(window.localStorage.getItem(LIST_WIDTH_KEY)).toBe("900");
    fireEvent.doubleClick(separator);
    expect(window.localStorage.getItem(LIST_WIDTH_KEY)).toBeNull();
    expect(separator.getAttribute("aria-valuenow")).toBe("413");
  });

  it("ignores a stored value it cannot read", () => {
    window.localStorage.setItem(LIST_WIDTH_KEY, "wide");
    withGridWidth(GRID_1280);
    const { separator } = renderSplit();
    expect(separator.getAttribute("aria-valuenow")).toBe("413");
  });
});
