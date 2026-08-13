import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import taskListResponse from "../test/fixtures/tasks.response.json";
import { TaskCount } from "./TaskCount";

describe("TaskCount", () => {
  it("renders the task count returned by the fixture", () => {
    render(<TaskCount count={taskListResponse.length} />);

    expect(screen.getByText(/The scoped API returned/)).toHaveTextContent(
      "The scoped API returned 3 tasks.",
    );
  });
});
