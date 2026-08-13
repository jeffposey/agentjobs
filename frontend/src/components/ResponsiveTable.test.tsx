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
