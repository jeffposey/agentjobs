import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import type { AnswerSubmission, AttachmentUpload, TaskDetailResponse, TaskRead } from "../api/types";
import { TaskDetail } from "./TaskDetail";
import { reviewLinksFor } from "./ReviewLinks";

/**
 * A review a human can act on without retyping an address (task-363).
 *
 * These drive the real panel rather than the card in isolation, because what has to be
 * true is a property of the rendered page: the URL an agent wrote into its handoff is
 * something a thumb can hit, and it is hoisted somewhere a reviewer sees before they
 * have read a paragraph. Every assertion reads the href the browser would follow --
 * "assert on rendered values, not on the presence of markup".
 *
 * The visual halves -- a legend clear of its border, a card that reads as the first
 * thing on the screen -- are not assertable here and were driven in a browser; the
 * evidence is on task-363.
 */

//: The task-240 handoff, 2026-09-06, which is the one that filed this task. Three
//: addresses, one of them a specific task page, all of them dead text at the time.
const PROMPT = [
  "The Tasks-surface sandbox is up on its own port with throwaway data.",
  "",
  "Desktop: http://127.0.0.1:8910/app/",
  "Tablet:  http://127.0.0.1:8910/app/?w=1024",
  "For the third check, open http://127.0.0.1:8910/app/p/sandbox-shell/tasks/task-143.",
  "",
  "Stop it with `agentjobs sandbox stop`.",
].join("\n");

function task(overrides: Partial<TaskRead> = {}): TaskRead {
  return {
    schema: 2,
    id: "task-240",
    title: "A sandbox for driving the whole Tasks-surface shell",
    created: "2026-09-06T05:00:00Z",
    updated: "2026-09-06T21:00:00Z",
    lifecycle: "active",
    ball: "human",
    ball_reason: "review",
    ball_prompt: PROMPT,
    display_status: "Waiting for review",
    priority: "high",
    category: "frontend",
    tags: [],
    assignment: { owner: "claude", eligible: [] },
    spec: { summary: "Summary.", description: "Description." },
    log: [],
    ...overrides,
  } as TaskRead;
}

function renderPanel(value: TaskRead = task()) {
  render(
    <MemoryRouter>
      <TaskDetail
        detail={
          {
            task: value,
            parent_task: null,
            children: [],
            needs: [],
            blocks: [],
            related: [],
            child_dependency_edges: [],
            identity: { ok: true, user: "Jeff Posey", problem: null, detail: "" },
          } as TaskDetailResponse
        }
        projectId="agentjobs"
        onApprove={vi.fn(async (_note: string | null) => undefined)}
        onSendBack={vi.fn(
          async (
            _reason: "revise" | "answer" | "redirect" | "hold",
            _feedback: string,
            _attachments: Array<AttachmentUpload>,
            _answers: Array<AnswerSubmission>,
          ) => undefined,
        )}
        onReject={vi.fn(async () => undefined)}
        onPromote={vi.fn(async () => undefined)}
        onResume={vi.fn(async (_note: string | null) => undefined)}
        onAddNote={vi.fn(async (_body: string) => undefined)}
      />
    </MemoryRouter>,
  );
}

/** The hrefs of a region's anchors, in document order. */
function hrefs(container: HTMLElement): Array<string> {
  return [...container.querySelectorAll("a[href]")].map((a) => a.getAttribute("href") ?? "");
}

describe("URLs in a handoff are tappable", () => {
  it("renders every address in the prompt as a link, not as text", () => {
    renderPanel();

    const panel = screen.getByRole("region", { name: "Review actions" });
    for (const url of [
      "http://127.0.0.1:8910/app/",
      "http://127.0.0.1:8910/app/?w=1024",
      "http://127.0.0.1:8910/app/p/sandbox-shell/tasks/task-143",
    ]) {
      expect(hrefs(panel)).toContain(url);
    }
  });

  it("does not carry the sentence's full stop into the address", () => {
    // The third URL ends a sentence. A link that 404s on a trailing dot is the same
    // defect wearing a different hat.
    renderPanel();

    const panel = screen.getByRole("region", { name: "Review actions" });
    expect(hrefs(panel)).not.toContain(
      "http://127.0.0.1:8910/app/p/sandbox-shell/tasks/task-143.",
    );
  });

  it("opens external addresses safely", () => {
    renderPanel();

    const panel = screen.getByRole("region", { name: "Review actions" });
    for (const anchor of panel.querySelectorAll("a[href^='http']")) {
      expect(anchor.getAttribute("rel")).toBe("noopener noreferrer");
      expect(anchor.getAttribute("target")).toBe("_blank");
    }
  });

  it("leaves agent-authored markup as text", () => {
    // Not a licence to interpret Markdown or HTML out of a task record (task-326).
    renderPanel(task({ ball_prompt: "<b>bold</b> and [a link](http://example.test/x)" }));

    const panel = screen.getByRole("region", { name: "Review actions" });
    expect(panel.querySelector("b")).toBeNull();
    expect(within(panel).getByText(/<b>bold<\/b>/)).toBeVisible();
    expect(hrefs(panel)).toContain("http://example.test/x");
  });

  it("renders no relative href, so nothing escapes the router basename", () => {
    // A raw anchor to an in-app path drops "/app" and lands on the legacy Jinja UI --
    // see InAppLinks.test.tsx. The prompt below is the shape that would cause it.
    renderPanel(task({ ball_prompt: "Check /p/agentjobs/tasks/task-240 before approving." }));

    const panel = screen.getByRole("region", { name: "Review actions" });
    expect(hrefs(panel).filter((href) => href.startsWith("/"))).toEqual([]);
  });
});

describe("the review links card", () => {
  it("lists the prompt's addresses above the verbs", () => {
    renderPanel();

    const card = screen.getByRole("navigation", { name: "Links for this review" });
    expect(hrefs(card)).toEqual([
      "http://127.0.0.1:8910/app/",
      "http://127.0.0.1:8910/app/?w=1024",
      "http://127.0.0.1:8910/app/p/sandbox-shell/tasks/task-143",
    ]);

    // Above the verb row on the page, which is the whole point of hoisting it.
    const approve = screen.getByRole("button", { name: /Approve/ });
    expect(card.compareDocumentPosition(approve) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("is absent when the handoff named no address", () => {
    renderPanel(task({ ball_prompt: "Read the diff and approve." }));

    expect(screen.queryByRole("navigation", { name: "Links for this review" })).toBeNull();
  });

  it("hoists a pr or build link and titles it, leaving a doc link where it was", () => {
    const rows = reviewLinksFor({
      ball_prompt: "Approve when the run is green.",
      links: [
        { url: "https://example.test/pull/9", rel: "pr", title: "PR #9" },
        { url: "https://example.test/build/3", rel: "build", title: null },
        { url: "https://example.test/handbook", rel: "doc", title: "Handbook" },
      ],
    });

    expect(rows).toEqual([
      { url: "https://example.test/pull/9", title: "PR #9" },
      { url: "https://example.test/build/3", title: null },
    ]);
  });

  it("does not list the same address twice when it is in both places", () => {
    const rows = reviewLinksFor({
      ball_prompt: "The build: https://example.test/build/3",
      links: [{ url: "https://example.test/build/3", rel: "build", title: "Build 3" }],
    });

    expect(rows).toEqual([{ url: "https://example.test/build/3", title: null }]);
  });
});
