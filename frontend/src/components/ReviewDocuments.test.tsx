import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { describe, expect, it } from "vitest";

import { client } from "../api/generated/client.gen";
import type { TaskRead } from "../api/types";
import { apiMockServer } from "../test/api-mock";
import { setViewport } from "../test/viewport";
import { ReviewDocuments } from "./ReviewDocuments";

/**
 * task-594: the review panel's "Documents under review".
 *
 * Assertions are on the text a reviewer reads -- the rendered heading, the table cell,
 * the refusal sentence -- never on the presence of markup. How it looks at 390px is a
 * screenshot on the task record, taken against a real server and a real branch.
 */

const DESIGN = [
  "# The design",
  "",
  "One **decision** per section.",
  "",
  "| Option | Verdict |",
  "|---|---|",
  "| Merge on plan | Rejected |",
  "",
  "[the spec](https://example.com/spec) and [a sibling](../other.md)",
  "",
  "<script>window.pwned = true</script>",
  "",
  "![a diagram](https://tracker.example.com/pixel.png)",
].join("\n");

function reviewTask(overrides: Partial<TaskRead> = {}): TaskRead {
  return {
    schema: 2,
    id: "task-594",
    title: "A design",
    created: "2026-09-25T08:00:00Z",
    updated: "2026-09-25T09:00:00Z",
    lifecycle: "active",
    ball: "human",
    ball_reason: "review",
    ball_prompt: "Review the design.",
    priority: "high",
    category: "ux",
    tags: [],
    assignment: { owner: "claude", eligible: [] },
    spec: { summary: "A design." },
    deliverables: [
      { path: "docs/design.md", note: "the design" },
      { path: "src/app.py" },
    ],
    branches: [{ name: "feat/task-594-design", status: "active" }],
    log: [],
    ...overrides,
  } as TaskRead;
}

function renderDocuments(task: TaskRead) {
  client.setConfig({ baseUrl: "http://localhost" });
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <ReviewDocuments task={task} projectId="agentjobs" />
    </QueryClientProvider>,
  );
}

function serveDocument(body: object, status = 200) {
  let requests = 0;
  apiMockServer.use(
    http.get("*/api/projects/agentjobs/tasks/task-594/deliverables/0", () => {
      requests += 1;
      return HttpResponse.json(body, { status });
    }),
  );
  return () => requests;
}

const DOCUMENT = {
  index: 0,
  path: "docs/design.md",
  branch: "feat/task-594-design",
  commit: "ab12cd34",
  size: DESIGN.length,
  text: DESIGN,
};

describe("ReviewDocuments", () => {
  it("renders a Markdown deliverable with where it was read from", async () => {
    serveDocument(DOCUMENT);
    renderDocuments(reviewTask());

    const section = screen.getByRole("region", { name: "Documents under review" });
    expect(await within(section).findByRole("heading", { name: "The design" })).toBeInTheDocument();
    expect(within(section).getByText("decision").tagName).toBe("STRONG");
    expect(within(section).getByRole("cell", { name: "Rejected" })).toBeInTheDocument();
    expect(section).toHaveTextContent("Read from feat/task-594-design at ab12cd34");
  });

  it("makes only absolute links anchors, and renders no HTML and no image", async () => {
    serveDocument(DOCUMENT);
    renderDocuments(reviewTask());
    const section = screen.getByRole("region", { name: "Documents under review" });
    await within(section).findByRole("heading", { name: "The design" });

    expect(within(section).getByRole("link", { name: "the spec" })).toHaveAttribute("href", "https://example.com/spec");
    expect(within(section).queryByRole("link", { name: "a sibling" })).toBeNull();
    expect(within(section).getByText("a sibling")).toHaveAttribute("title", "../other.md");
    expect(section.querySelector("script")).toBeNull();
    expect(section.querySelector("img")).toBeNull();
    expect(section).toHaveTextContent("[image: a diagram]");
  });

  it("lists a deliverable that is not Markdown as a path only, and asks for nothing", async () => {
    const requests = serveDocument(DOCUMENT);
    renderDocuments(reviewTask());
    const section = screen.getByRole("region", { name: "Documents under review" });
    await within(section).findByRole("heading", { name: "The design" });

    expect(section).toHaveTextContent("src/app.pyNot Markdown; listed, not rendered.");
    expect(requests()).toBe(1);
  });

  it("is collapsed on a phone and reads the branch only when opened", async () => {
    setViewport(390, 844);
    const requests = serveDocument(DOCUMENT);
    renderDocuments(reviewTask());

    const section = screen.getByRole("region", { name: "Documents under review" });
    expect(within(section).queryByRole("heading", { name: "The design" })).toBeNull();
    expect(requests()).toBe(0);

    fireEvent.click(within(section).getByText("docs/design.md"));
    expect(await within(section).findByRole("heading", { name: "The design" })).toBeInTheDocument();
    expect(requests()).toBe(1);
  });

  it.each([
    ["no_active_branch", 409, "task-594 lists no active branch, so there is nowhere to read docs/design.md from."],
    ["several_active_branches", 409, "task-594 lists 2 active branches (a, b); which one holds the document is not something to guess."],
    ["file_missing", 404, "docs/design.md does not exist on feat/x at ab12cd34."],
    ["too_large", 413, "docs/design.md is 600,000 bytes, over the 524,288-byte limit the review panel renders. Read it on the branch."],
  ])("shows the %s refusal in the document's place", async (code, status, message) => {
    serveDocument({ code, message, detail: message, retryable: false }, status);
    renderDocuments(reviewTask());

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(`${message} (${code})`);
  });

  it.each([
    ["the ball is with the agent", { ball: "agent", ball_reason: "work" }],
    ["the human is answering a decision", { ball: "human", ball_reason: "decision" }],
    ["there are no deliverables", { deliverables: [] }],
  ] as const)("is absent when %s", (_why, overrides) => {
    renderDocuments(reviewTask(overrides as Partial<TaskRead>));
    expect(screen.queryByRole("region", { name: "Documents under review" })).toBeNull();
  });

  it.each(["approval", "plan"])("appears at the %s gate", async (reason) => {
    serveDocument(DOCUMENT);
    renderDocuments(reviewTask({ ball_reason: reason } as Partial<TaskRead>));
    expect(await screen.findByRole("heading", { name: "The design" })).toBeInTheDocument();
  });
});
