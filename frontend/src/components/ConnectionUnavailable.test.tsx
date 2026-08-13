import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ConnectionUnavailable } from "./ConnectionUnavailable";

describe("ConnectionUnavailable", () => {
  it("refuses to present cached task data as current while offline", () => {
    render(<ConnectionUnavailable offline />);

    expect(screen.getByRole("heading", { name: "You’re offline" })).toBeVisible();
    expect(screen.getByRole("alert")).toHaveTextContent(
      "No task data is shown because cached assignments could be out of date.",
    );
  });

  it("explains when the host cannot be reached", () => {
    render(<ConnectionUnavailable offline={false} />);

    expect(screen.getByRole("heading", { name: "AgentJobs cannot be reached" })).toBeVisible();
    expect(screen.getByText(/wake the computer running AgentJobs/)).toBeVisible();
  });
});
