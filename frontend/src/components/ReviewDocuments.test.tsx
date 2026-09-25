import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { beforeAll, describe, expect, it } from "vitest";

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

/**
 * How long a wait that includes rendering a document may take (flake register row 22).
 *
 * Waiting for the heading means waiting for a fetch, a Suspense boundary and a Markdown
 * parse. Under the gate's full vitest run the first such test took 840-1002 ms even
 * with the renderer preloaded, and 1561 ms without, so testing-library's 1 s default
 * turned CPU contention into a red. These tests check what renders, not how fast.
 */
const RENDERED = { timeout: 5_000 };

describe("ReviewDocuments", () => {
  // The renderer is a lazy chunk. Loading it here, once, takes its import (143 ms on an
  // idle machine) out of every wait below rather than charging it to the first test.
  beforeAll(async () => {
    await import("./MarkdownDocument");
  });

  it("renders a Markdown deliverable with where it was read from", async () => {
    serveDocument(DOCUMENT);
    renderDocuments(reviewTask());

    const section = screen.getByRole("region", { name: "Documents under review" });
    expect(await within(section).findByRole("heading", { name: "The design" }, RENDERED)).toBeInTheDocument();
    expect(within(section).getByText("decision").tagName).toBe("STRONG");
    expect(within(section).getByRole("cell", { name: "Rejected" })).toBeInTheDocument();
    expect(section).toHaveTextContent("Read from feat/task-594-design at ab12cd34");
  });

  it("makes only absolute links anchors, and renders no HTML and no image", async () => {
    serveDocument(DOCUMENT);
    renderDocuments(reviewTask());
    const section = screen.getByRole("region", { name: "Documents under review" });
    await within(section).findByRole("heading", { name: "The design" }, RENDERED);

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
    await within(section).findByRole("heading", { name: "The design" }, RENDERED);

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
    expect(await within(section).findByRole("heading", { name: "The design" }, RENDERED)).toBeInTheDocument();
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

  it("opens a document full screen, and the X puts the page back as it was", async () => {
    setViewport(390, 844);
    serveDocument(DOCUMENT);
    renderDocuments(reviewTask());
    const maximize = screen.getByRole("button", { name: "Read docs/design.md full screen" });

    fireEvent.click(maximize);
    const view = screen.getByRole("dialog", { name: "docs/design.md" });
    expect(await within(view).findByRole("heading", { name: "The design" }, RENDERED)).toBeInTheDocument();
    expect(view).toHaveTextContent("Read from feat/task-594-design at ab12cd34");
    const close = within(view).getByRole("button", { name: "Close full screen" });
    expect(close).toHaveFocus();
    expect(document.body.style.overflow).toBe("hidden");
    // Maximizing is not also a tap on the collapsed row: it stays collapsed underneath.
    const section = screen.getByRole("region", { name: "Documents under review" });
    expect(within(section).queryByRole("heading", { name: "The design" })).toBeNull();

    fireEvent.click(close);
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.body.style.overflow).toBe("");
    expect(maximize).toHaveFocus();
  });

  it("closes the full-screen view on Escape", async () => {
    serveDocument(DOCUMENT);
    renderDocuments(reviewTask());

    fireEvent.click(screen.getByRole("button", { name: "Read docs/design.md full screen" }));
    await within(screen.getByRole("dialog")).findByRole("heading", { name: "The design" }, RENDERED);
    fireEvent.keyDown(document, { key: "Escape" });

    expect(screen.queryByRole("dialog")).toBeNull();
    // The inline copy, open on a wide screen, is still there.
    expect(screen.getByRole("heading", { name: "The design" })).toBeInTheDocument();
  });

  it("offers no full screen for a deliverable that is not Markdown", () => {
    serveDocument(DOCUMENT);
    renderDocuments(reviewTask());
    expect(screen.queryByRole("button", { name: "Read src/app.py full screen" })).toBeNull();
  });

  it.each(["approval", "plan"])("appears at the %s gate", async (reason) => {
    serveDocument(DOCUMENT);
    renderDocuments(reviewTask({ ball_reason: reason } as Partial<TaskRead>));
    expect(await screen.findByRole("heading", { name: "The design" }, RENDERED)).toBeInTheDocument();
  });
});
