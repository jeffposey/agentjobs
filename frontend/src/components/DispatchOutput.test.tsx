import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { describe, expect, it } from "vitest";

import type { DispatchRunView } from "../api/types";
import { client } from "../api/generated/client.gen";
import { apiMockServer } from "../test/api-mock";
import {
  DispatchRunOutput,
  TAIL_POLL_MS,
  diffLabel,
  emptyOutputNote,
  failureLabel,
  sourceNote,
  tailPollInterval,
  transcriptUnavailable,
} from "./DispatchOutput";

/**
 * These are about two requirements. Jeff's on task-157: output has to be readable
 * *while* a run is going, in one collapsible place that still holds it afterwards. And
 * his on task-023: readable has to mean readable. The panel used to tail the session's
 * terminal, which is a repaint rather than a log, so an approval dialog rendered as
 * `NewMCPserverfoundinthisproject:agentjobs`.
 *
 * They assert on the text a browser will show, never on the markup carrying it -- the
 * broken panel had every element and attribute it was supposed to have.
 */

const TAIL_URL = "*/api/projects/sandbox/dispatch/runs/run_abc123/tail";
const TRANSCRIPT_URL = "*/api/projects/sandbox/dispatch/runs/run_abc123/transcript";

function run(overrides: Partial<DispatchRunView> = {}): DispatchRunView {
  return {
    run_id: "run_abc123",
    task_id: "task-001",
    project_id: "sandbox",
    mode: "session",
    posture: "autonomous",
    status: "running",
    outcome: null,
    session_id: "b55b35ad",
    started_at: "2026-08-18T10:00:00+00:00",
    elapsed_seconds: 42,
    live: true,
    caused_by: 3,
    output_url: "/api/projects/sandbox/dispatch/runs/run_abc123/output",
    ...overrides,
  };
}

function tail(overrides: Record<string, unknown> = {}) {
  return {
    run_id: "run_abc123",
    live: true,
    source: "session-transcript",
    lines: 40,
    text: "Reading the task record\nMaking a change",
    updated_at: "2026-08-18T10:01:00+00:00",
    ...overrides,
  };
}

function transcript(overrides: Record<string, unknown> = {}) {
  return {
    run_id: "run_abc123",
    live: true,
    source: "session-jsonl",
    note: "",
    entries: [{ kind: "narration", text: "Reading the task record.", calls: [] }],
    total_entries: 1,
    truncated: false,
    updated_at: "2026-08-18T10:01:00+00:00",
    ...overrides,
  };
}

function toolEntry(overrides: Record<string, unknown> = {}) {
  return {
    kind: "tools",
    text: "",
    summary: "Edited DependencyState.tsx, ran 4 commands",
    added: 14,
    removed: 1,
    failed: 0,
    calls: [
      {
        name: "Bash",
        title: "Run the suite",
        detail: "poetry run pytest -q",
        output: "2 passed",
        failed: false,
        added: 0,
        removed: 0,
      },
    ],
    ...overrides,
  };
}

/** Both endpoints answered, so a test only has to say what it is actually about. */
function serve({ tailBody = tail(), transcriptBody = transcript() } = {}) {
  client.setConfig({ baseUrl: "http://localhost" });
  apiMockServer.use(
    http.get(TAIL_URL, () => HttpResponse.json(tailBody)),
    http.get(TRANSCRIPT_URL, () => HttpResponse.json(transcriptBody)),
  );
}

function renderOutput(view: DispatchRunView) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <DispatchRunOutput run={view} />
    </QueryClientProvider>,
  );
}

describe("DispatchRunOutput", () => {
  it("shows a live run's output on the task page without anyone clicking through", async () => {
    serve();

    renderOutput(run());

    expect(screen.getByRole("button", { name: /Output/ })).toHaveAttribute("aria-expanded", "true");
    expect(await screen.findByText(/Reading the task record\./)).toBeInTheDocument();
  });

  it("holds the finished output in the same section, collapsed until asked for", async () => {
    serve({
      transcriptBody: transcript({
        live: false,
        entries: [{ kind: "narration", text: "Exiting cleanly.", calls: [] }],
      }),
    });

    renderOutput(run({ live: false, status: "finished", outcome: "completed" }));

    const toggle = screen.getByRole("button", { name: /Output/ });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText(/Exiting cleanly/)).not.toBeInTheDocument();

    fireEvent.click(toggle);

    expect(await screen.findByText(/Exiting cleanly/)).toBeInTheDocument();
  });

  it("asks for nothing at all while it is collapsed", async () => {
    client.setConfig({ baseUrl: "http://localhost" });
    let requests = 0;
    apiMockServer.use(
      http.get(TAIL_URL, () => {
        requests += 1;
        return HttpResponse.json(tail({ live: false }));
      }),
      http.get(TRANSCRIPT_URL, () => {
        requests += 1;
        return HttpResponse.json(transcript({ live: false }));
      }),
    );

    renderOutput(run({ live: false }));

    await waitFor(() => expect(screen.getByRole("button", { name: /Output/ })).toBeVisible());
    expect(requests).toBe(0);
  });
});

describe("the readable view", () => {
  it("renders a dialog as sentences rather than as one run-together string", async () => {
    // Verbatim from the defect: the panel showed
    // `NewMCPserverfoundinthisproject:agentjobs` because the terminal draws each space
    // by moving the cursor rather than by emitting one.
    serve({
      transcriptBody: transcript({
        entries: [
          {
            kind: "narration",
            text: "New MCP server found in this project: agentjobs",
            calls: [],
          },
        ],
      }),
    });

    renderOutput(run());

    expect(
      await screen.findByText("New MCP server found in this project: agentjobs"),
    ).toBeInTheDocument();
  });

  it("separates what the agent said from what it did", async () => {
    serve({
      transcriptBody: transcript({
        entries: [
          { kind: "prompt", text: "work task-023", calls: [] },
          { kind: "narration", text: "Reading the record first.", calls: [] },
          toolEntry(),
        ],
        total_entries: 3,
      }),
    });

    renderOutput(run());

    expect(await screen.findByText("work task-023")).toBeInTheDocument();
    expect(screen.getByText("Reading the record first.")).toBeInTheDocument();
    expect(screen.getByText("Edited DependencyState.tsx, ran 4 commands")).toBeInTheDocument();
  });

  it("summarizes a run of calls, with the detail available on demand", async () => {
    serve({ transcriptBody: transcript({ entries: [toolEntry()] }) });

    renderOutput(run());

    const group = await screen.findByRole("button", {
      name: /Edited DependencyState\.tsx, ran 4 commands/,
    });
    expect(screen.getByText("+14 -1")).toBeInTheDocument();
    // Collapsed by default: the summary is what makes the panel scannable, and dumping
    // every call would reproduce the wall of text this replaced.
    expect(screen.queryByText("poetry run pytest -q")).not.toBeInTheDocument();

    fireEvent.click(group);

    expect(screen.getByText("poetry run pytest -q")).toBeInTheDocument();
    expect(screen.getByText("2 passed")).toBeInTheDocument();
  });

  it("says in words that a call failed, on the summary and on the call itself", async () => {
    serve({
      transcriptBody: transcript({
        entries: [
          toolEntry({
            summary: "Ran 2 commands",
            added: 0,
            removed: 0,
            failed: 1,
            calls: [
              {
                name: "Bash",
                title: "Build",
                detail: "npm run build",
                output: "error TS2345: argument of type 'x'",
                failed: true,
                added: 0,
                removed: 0,
              },
            ],
          }),
        ],
      }),
    });

    renderOutput(run());

    expect(await screen.findByText("1 failed")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Ran 2 commands/ }));

    expect(screen.getByText("failed")).toBeInTheDocument();
    expect(screen.getByText(/error TS2345/)).toBeInTheDocument();
  });

  it("says how much it left off rather than silently showing the end", async () => {
    serve({
      transcriptBody: transcript({
        entries: [{ kind: "narration", text: "The last thing.", calls: [] }],
        total_entries: 137,
        truncated: true,
      }),
    });

    renderOutput(run());

    expect(await screen.findByText(/Showing the last 1 of 137 steps/)).toBeInTheDocument();
  });

  it("does not report a live run as dead when its transcript cannot be read", async () => {
    client.setConfig({ baseUrl: "http://localhost" });
    apiMockServer.use(
      http.get(TRANSCRIPT_URL, () => HttpResponse.json({ detail: "boom" }, { status: 500 })),
      http.get(TAIL_URL, () => HttpResponse.json(tail())),
    );

    renderOutput(run());

    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent(/could not be read just now/),
    );
  });
});

describe("degrading to the raw terminal", () => {
  it("falls back to the captured bytes, and says why, when nothing structured exists", async () => {
    serve({
      transcriptBody: transcript({
        source: "none",
        note: "This run was a batch command rather than a session, so there is no structured transcript.",
        entries: [],
        total_entries: 0,
      }),
      tailBody: tail({ source: "captured-output", text: "the agent said this" }),
    });

    renderOutput(run());

    expect(await screen.findByText(/batch command rather than a session/)).toBeInTheDocument();
    // The tail is only asked for once the readable view has answered that it has
    // nothing, so this arrives a beat later than the note does.
    expect(await screen.findByText(/the agent said this/)).toBeInTheDocument();
  });

  it("says why there is nothing yet rather than showing an empty box", async () => {
    serve({
      transcriptBody: transcript({ source: "none", note: "Not yet.", entries: [] }),
      tailBody: tail({ source: "none", text: "" }),
    });

    renderOutput(run());

    expect(await screen.findByText(/Nothing captured yet/)).toBeInTheDocument();
  });

  it("keeps the unparsed bytes one click away, because sometimes they are the evidence", async () => {
    serve({ tailBody: tail({ text: "Reading the task record\nMaking a change" }) });

    renderOutput(run());

    expect(await screen.findByText(/Reading the task record\./)).toBeInTheDocument();
    expect(screen.queryByText(/Making a change/)).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Show raw terminal/ }));

    expect(await screen.findByText(/Making a change/)).toBeInTheDocument();
    expect(screen.queryByText("Reading the task record.")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Show readable/ })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });
});

describe("the panel's own rules", () => {
  it("never polls faster than the poller that produces what it reads", () => {
    // The raw text comes from a file the session poller writes by shelling out to the
    // runner. A browser polling faster would read the same bytes back sooner; a browser
    // fetching it *itself* would spawn a process per watcher, which is the thing this
    // interval exists to keep impossible.
    expect(TAIL_POLL_MS).toBeGreaterThanOrEqual(10_000);
    expect(tailPollInterval(true)).toBe(TAIL_POLL_MS);
    expect(tailPollInterval(false)).toBe(false);
  });

  it("distinguishes 'nothing yet' from 'nothing at all'", () => {
    expect(emptyOutputNote(true, "none")).toMatch(/Nothing captured yet/);
    expect(emptyOutputNote(false, "none")).toMatch(/captured no output/);
    expect(emptyOutputNote(true, "session-transcript")).toBe("");
  });

  it("names where the text came from, because the two sources differ", () => {
    expect(sourceNote("session-transcript")).toMatch(/session's own transcript/);
    expect(sourceNote("captured-output")).toMatch(/stdout and stderr/);
  });

  it("shows counts only when something actually changed", () => {
    expect(diffLabel(14, 1)).toBe("+14 -1");
    expect(diffLabel(0, 0)).toBe("");
  });

  it("counts failures in words, and says nothing when there are none", () => {
    expect(failureLabel(0)).toBe("");
    expect(failureLabel(1)).toBe("1 failed");
    expect(failureLabel(3)).toBe("3 failed");
  });

  it("treats every source but the structured one as a reason to degrade", () => {
    expect(transcriptUnavailable("session-jsonl")).toBe(false);
    expect(transcriptUnavailable("none")).toBe(true);
    expect(transcriptUnavailable(undefined)).toBe(true);
  });
});
