import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import type { AnswerSubmission, AttachmentUpload, TaskDetailResponse, TaskRead } from "../api/types";
import { TaskDetail } from "./TaskDetail";
import { reviewPromptFor } from "./ReviewLinks";

/**
 * A review a human can act on without retyping an address, and without reading it
 * twice (task-363).
 *
 * These drive the real panel rather than the card in isolation, because what has to be
 * true is a property of the rendered page: the URL an agent wrote into its handoff is
 * something a thumb can hit, it is somewhere a reviewer sees before they have read a
 * paragraph, it says what it is, and it is on the screen exactly once. Every assertion
 * reads the href the browser would follow -- "assert on rendered values, not on the
 * presence of markup".
 *
 * The visual halves -- a legend clear of its border, a card that reads as the first
 * thing on the screen -- are not assertable here and were driven in a browser; the
 * evidence is on task-363.
 */

//: The task-240 handoff of 2026-09-06 rewritten to the convention this pass
//: introduces: the addresses are on their own named lines, out of the prose.
const PROMPT = [
  "The Tasks-surface sandbox is up on its own port with throwaway data.",
  "",
  "Three checks; the third needs a task page rather than the list:",
  "  1. Open a task from the sidebar and confirm the list keeps its scroll position.",
  "  2. Narrow the window to phone width; the panel should restack.",
  "  3. Open the task page below and confirm the log expands.",
  "",
  "Desktop shell: http://127.0.0.1:8910/app/",
  "Tablet: http://127.0.0.1:8910/app/?w=1024",
  "Task page for check 3: http://127.0.0.1:8910/app/p/sandbox-shell/tasks/task-143",
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

function panel() {
  return screen.getByRole("region", { name: "Review actions" });
}

function card() {
  return screen.getByRole("navigation", { name: "Links for this review" });
}

describe("a review's links are in the card, and only there", () => {
  it("puts every named address in the card, with the name the author gave it", () => {
    renderPanel();

    expect(
      [...card().querySelectorAll("a")].map((a) => [
        a.getAttribute("href"),
        a.textContent?.trim(),
      ]),
    ).toEqual([
      ["http://127.0.0.1:8910/app/", "Desktop shellhttp://127.0.0.1:8910/app/"],
      ["http://127.0.0.1:8910/app/?w=1024", "Tablethttp://127.0.0.1:8910/app/?w=1024"],
      [
        "http://127.0.0.1:8910/app/p/sandbox-shell/tasks/task-143",
        "Task page for check 3http://127.0.0.1:8910/app/p/sandbox-shell/tasks/task-143",
      ],
    ]);
  });

  it("renders each address exactly once on the whole screen", () => {
    // The first pass showed them linkified in the prose *and* listed in the card,
    // which on a phone is a 60-character address costing three lines, twice.
    renderPanel();

    expect(hrefs(panel())).toEqual([
      "http://127.0.0.1:8910/app/",
      "http://127.0.0.1:8910/app/?w=1024",
      "http://127.0.0.1:8910/app/p/sandbox-shell/tasks/task-143",
    ]);
    expect(hrefs(card())).toEqual(hrefs(panel()));
  });

  it("takes the link lines out of the prose without disturbing the rest", () => {
    renderPanel();

    expect(panel().textContent).toContain("Open the task page below and confirm the log expands.");
    // The addresses are gone from the prose entirely, not merely un-anchored.
    const prose = panel().textContent?.replace(card().textContent ?? "", "") ?? "";
    expect(prose).not.toContain("127.0.0.1:8910");
  });

  it("is absent when the handoff named no address", () => {
    renderPanel(task({ ball_prompt: "Read the diff and approve." }));

    expect(screen.queryByRole("navigation", { name: "Links for this review" })).toBeNull();
  });

  it("opens external addresses safely", () => {
    renderPanel();

    for (const anchor of panel().querySelectorAll("a[href^='http']")) {
      expect(anchor.getAttribute("rel")).toBe("noopener noreferrer");
      expect(anchor.getAttribute("target")).toBe("_blank");
    }
  });

  it("puts the card above the verbs", () => {
    renderPanel();

    const approve = screen.getByRole("button", { name: /Approve/ });
    expect(card().compareDocumentPosition(approve) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });
});

describe("what counts as a link line", () => {
  const links = (prompt: string) => reviewPromptFor({ ball_prompt: prompt }).links;
  const prose = (prompt: string) => reviewPromptFor({ ball_prompt: prompt }).prose;

  it("takes a bulleted one, and a bare address on its own line", () => {
    expect(links("- Build: https://example.test/ci/9\nhttps://example.test/x")).toEqual([
      { url: "https://example.test/ci/9", name: "Build" },
      { url: "https://example.test/x", name: null },
    ]);
  });

  it("leaves an address inside a sentence where the author wrote it", () => {
    // Hoisting it would leave "Open  and confirm", and duplicating it is the thing
    // this pass removes. The convention resolves it at the source: link lines.
    const prompt = "Open https://example.test/x and confirm the log expands.";

    expect(links(prompt)).toEqual([]);
    expect(prose(prompt)).toBe(prompt);
  });

  it("leaves a line carrying two addresses alone", () => {
    const prompt = "Compare https://example.test/a with https://example.test/b";

    expect(links(prompt)).toEqual([]);
    expect(prose(prompt)).toBe(prompt);
  });

  it("does not treat a sentence ending in a colon-free address as a named link", () => {
    // "Open" is not a name, and inventing one from it would be a guess.
    expect(links("Open https://example.test/x")).toEqual([]);
  });

  it("collapses the blank lines a removed block leaves behind", () => {
    const prompt = ["Approve when green.", "", "A: https://example.test/a", "", ""].join("\n");

    expect(prose(prompt)).toBe("Approve when green.");
  });

  it("lists a repeated address once", () => {
    const prompt = "A: https://example.test/a\nAlso A: https://example.test/a";

    expect(links(prompt)).toEqual([{ url: "https://example.test/a", name: "A" }]);
  });
});

describe("links[] joins the card without displacing the prompt's", () => {
  it("hoists a pr and a build with their titles, leaving a doc link below", () => {
    expect(
      reviewPromptFor({
        ball_prompt: "Approve when the run is green.",
        links: [
          { url: "https://example.test/pull/9", rel: "pr", title: "PR #9" },
          { url: "https://example.test/build/3", rel: "build", title: null },
          { url: "https://example.test/handbook", rel: "doc", title: "Handbook" },
        ],
      }).links,
    ).toEqual([
      { url: "https://example.test/pull/9", name: "PR #9" },
      { url: "https://example.test/build/3", name: null },
    ]);
  });

  it("keeps the name the prompt gave an address it shares with links[]", () => {
    // The prompt's name was written for this review; the link's title was written for
    // the task's whole life. The review is what is on screen.
    expect(
      reviewPromptFor({
        ball_prompt: "Build for this branch: https://example.test/build/3",
        links: [{ url: "https://example.test/build/3", rel: "build", title: "Build 3" }],
      }).links,
    ).toEqual([{ url: "https://example.test/build/3", name: "Build for this branch" }]);
  });

  it("does not anchor an address in the prose that the card already holds", () => {
    // The one remaining way the same URL could be a tap target twice: mid-sentence in
    // the prompt and hoisted from links[]. It stays readable, and stays text.
    renderPanel(
      task({
        ball_prompt: "Open https://example.test/build/3 and read the failure.",
        links: [{ url: "https://example.test/build/3", rel: "build", title: "Build 3" }],
      }),
    );

    expect(hrefs(panel())).toEqual(["https://example.test/build/3"]);
    expect(hrefs(card())).toEqual(["https://example.test/build/3"]);
    expect(within(panel()).getByText(/Open .* and read the failure\./)).toBeVisible();
  });
});

describe("URLs elsewhere in the record are still tappable", () => {
  it("linkifies a log entry body", () => {
    renderPanel(
      task({
        ball_prompt: "Read the diff and approve.",
        log: [
          {
            id: 1,
            ts: "2026-09-06T21:00:00Z",
            actor: "claude",
            type: "progress",
            body: "The failing run is at https://example.test/ci/9.",
          },
        ],
      }),
    );

    const anchor = screen.getByRole("link", { name: "https://example.test/ci/9" });
    expect(anchor.getAttribute("rel")).toBe("noopener noreferrer");
  });

  it("leaves agent-authored markup as text", () => {
    // Not a licence to interpret Markdown or HTML out of a task record (task-326).
    renderPanel(task({ ball_prompt: "<b>bold</b> and [a link](http://example.test/x)" }));

    expect(panel().querySelector("b")).toBeNull();
    expect(within(panel()).getByText(/<b>bold<\/b>/)).toBeVisible();
    expect(hrefs(panel())).toContain("http://example.test/x");
  });

  it("renders no relative href, so nothing escapes the router basename", () => {
    // A raw anchor to an in-app path drops "/app" and lands on the legacy Jinja UI --
    // see InAppLinks.test.tsx. The prompt below is the shape that would cause it.
    renderPanel(task({ ball_prompt: "Check /p/agentjobs/tasks/task-240 before approving." }));

    expect(hrefs(panel()).filter((href) => href.startsWith("/"))).toEqual([]);
  });

  it("does not carry a sentence's full stop into an address", () => {
    renderPanel(task({ ball_prompt: "Read https://example.test/a/b." }));

    expect(hrefs(panel())).toEqual(["https://example.test/a/b"]);
  });
});
