import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ResponsiveCell, ResponsiveTable, ResponsiveTableRow } from "./ResponsiveTable";

describe("ResponsiveTable", () => {
  it("puts each mobile label on the cell that owns the value", () => {
    render(
      <ResponsiveTable aria-label="Example tasks">
        <tbody>
          <ResponsiveTableRow>
            <ResponsiveCell label="Task">task-123</ResponsiveCell>
            <ResponsiveCell label="Status">Ready</ResponsiveCell>
          </ResponsiveTableRow>
        </tbody>
      </ResponsiveTable>,
    );

    expect(screen.getByText("task-123").closest("td")).toHaveAttribute("data-label", "Task");
    expect(screen.getByText("Ready").closest("td")).toHaveAttribute("data-label", "Status");
  });
});

describe("ResponsiveTable column widths", () => {
  // The widths are the whole of the fixed-layout fix: with `table-layout: fixed` and
  // no `<colgroup>`, every column gets an equal share, which is worse for a six-column
  // table than the auto layout it replaced. task-341.
  it("emits one col per declared width, and leaves the flexible one unset", () => {
    const { container } = render(
      <ResponsiveTable aria-label="Example tasks" columns={["5rem", null, "12rem"]}>
        <tbody>
          <ResponsiveTableRow>
            <ResponsiveCell label="Queue">1</ResponsiveCell>
            <ResponsiveCell label="Task">task-123</ResponsiveCell>
            <ResponsiveCell label="Status">Ready</ResponsiveCell>
          </ResponsiveTableRow>
        </tbody>
      </ResponsiveTable>,
    );

    const cols = [...container.querySelectorAll("col")];
    expect(cols.map((col) => col.style.width)).toEqual(["5rem", "", "12rem"]);
  });

  it("emits no colgroup for a table that did not ask for one", () => {
    const { container } = render(
      <ResponsiveTable aria-label="Example tasks">
        <tbody>
          <ResponsiveTableRow>
            <ResponsiveCell label="Task">task-123</ResponsiveCell>
          </ResponsiveTableRow>
        </tbody>
      </ResponsiveTable>,
    );

    expect(container.querySelector("colgroup")).toBeNull();
  });
});
