import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { AcceptanceCriterion, LogEntry } from "../api/generated";
import {
  AcceptanceSection,
  checkSentence,
  formatDuration,
  hasExecutableChecks,
  latestCheckOutcomes,
} from "./AcceptanceChecks";

afterEach(cleanup);

/**
 * The check results on the acceptance list (task-152).
 *
 * Every rendering assertion here reads a **value a person would read** rather than an
 * attribute. `data-check-standing="failed"` renders just as happily while the sentence
 * beside it says `AcceptanceStatus.FAILED`, and one of the two is what somebody
 * deciding whether their branch is done actually looks at.
 */

function criterion(overrides: Partial<AcceptanceCriterion> & { id: string }): AcceptanceCriterion {
  return { text: `Text of ${overrides.id}`, status: "pending", ...overrides };
}

function checkPass(
  id: number,
  results: Array<Record<string, unknown>>,
  unchecked: Array<string> = [],
): LogEntry {
  return {
    id,
    ts: `2026-09-22T1${id}:00:00Z`,
    actor: "Jeff Posey",
    type: "check_result",
    body: null,
    data: { chain_id: null, iteration: null, results, unchecked },
  };
}

describe("reading the newest result per criterion", () => {
  it("takes the newest pass that named the criterion, not the newest pass", () => {
    // ac-2 was decided by the older pass and is absent from the newer one, which is
    // what a task looks like after a criterion's check is removed and re-added, or
    // after a pass that timed out part-way through.
    const log = [
      checkPass(1, [
        { id: "ac-1", status: "failed", exit_code: 1, duration_seconds: 0.5 },
        { id: "ac-2", status: "met", exit_code: 0, duration_seconds: 2 },
      ]),
      checkPass(2, [{ id: "ac-1", status: "met", exit_code: 0, duration_seconds: 0.4 }]),
    ];

    const standings = latestCheckOutcomes(log);
    // ac-1 carries the *newer* pass's numbers, not the older one's.
    expect(standings.get("ac-1")).toMatchObject({
      kind: "passed",
      outcome: { duration_seconds: 0.4 },
    });
    expect(standings.get("ac-2")).toMatchObject({
      kind: "passed",
      outcome: { duration_seconds: 2 },
    });
  });

  it("retires an older result when a newer pass reports the criterion as unchecked", () => {
    // The check was removed between the two passes. Carrying the old `met` forward
    // would show a green result for a command that no longer exists on the record.
    const log = [
      checkPass(1, [{ id: "ac-1", status: "met", exit_code: 0, duration_seconds: 1 }]),
      checkPass(2, [], ["ac-1"]),
    ];

    expect(latestCheckOutcomes(log).get("ac-1")).toEqual({ kind: "never-run" });
  });

  it("ignores entries that are not check results, and malformed outcomes inside one", () => {
    const log = [
      { id: 1, ts: "2026-09-22T10:00:00Z", actor: "claude", type: "progress", body: "Worked." },
      checkPass(2, [
        { id: "ac-1", status: "wat", exit_code: 0, duration_seconds: 1 },
        { status: "met", exit_code: 0, duration_seconds: 1 },
        { id: "ac-2", status: "met", exit_code: 0, duration_seconds: 1 },
      ]),
    ] as Array<LogEntry>;

    const standings = latestCheckOutcomes(log);
    expect(standings.has("ac-1")).toBe(false);
    expect(standings.get("ac-2")).toMatchObject({ kind: "passed" });
  });

  it("finds no standing at all on a task nothing has ever checked", () => {
    expect(latestCheckOutcomes([]).size).toBe(0);
  });
});

describe("the sentence a standing is worth", () => {
  it("keeps never-run apart from failed in words, not only in a class", () => {
    expect(checkSentence({ kind: "never-run" })).toBe("Check not yet run");
  });

  it("names the exit code on a failure that exited", () => {
    expect(
      checkSentence({
        kind: "failed",
        outcome: { id: "ac-1", status: "failed", exit_code: 2, duration_seconds: 1.25, cause: null, output_tail: null },
      }),
    ).toBe("Check failed in 1.3s — exited 2");
  });

  it("explains a failure with no exit code in prose rather than by its cause enum", () => {
    const sentence = checkSentence({
      kind: "failed",
      outcome: { id: "ac-1", status: "failed", exit_code: null, duration_seconds: 900, cause: "timeout", output_tail: null },
    });
    expect(sentence).toBe("Check failed in 15m 0s — it ran out of time");
    expect(sentence).not.toContain("timeout");
  });

  it("renders a sub-second check as milliseconds, never as a duration that reads as zero", () => {
    expect(formatDuration(0.042)).toBe("42ms");
    expect(formatDuration(0.0001)).toBe("1ms");
    expect(formatDuration(12.34)).toBe("12.3s");
  });
});

describe("hasExecutableChecks", () => {
  it("is false for prose-only acceptance and true once one criterion carries an argv", () => {
    expect(hasExecutableChecks([criterion({ id: "ac-1" })])).toBe(false);
    expect(hasExecutableChecks([criterion({ id: "ac-1" }), criterion({ id: "ac-2", check: ["true"] })])).toBe(true);
    expect(hasExecutableChecks(null)).toBe(false);
  });
});

describe("the acceptance list as rendered", () => {
  const acceptance = [
    criterion({ id: "ac-1", text: "The gate is green.", check: ["pytest", "-q"], status: "met" }),
    criterion({ id: "ac-2", text: "Lint is clean.", check: ["ruff", "check", "."], status: "failed" }),
    criterion({ id: "ac-3", text: "It reads well on a phone." }),
    criterion({ id: "ac-4", text: "Types check.", check: ["mypy", "."] }),
  ];
  const log = [
    checkPass(1, [
      { id: "ac-1", status: "met", exit_code: 0, duration_seconds: 41.2 },
      {
        id: "ac-2",
        status: "failed",
        exit_code: 1,
        duration_seconds: 0.8,
        output_tail: "src/app.py:12:1: F401 imported but unused",
      },
    ]),
  ];

  it("shows a word a person reads for each of passed, failed and never run", () => {
    render(<AcceptanceSection acceptance={acceptance} log={log} onRunChecks={vi.fn()} />);

    expect(screen.getByText("Check passed in 41.2s")).toBeTruthy();
    expect(screen.getByText("Check failed in 800ms — exited 1")).toBeTruthy();
    expect(screen.getByText("Check not yet run")).toBeTruthy();
    // The one criterion with no check gets no check line invented for it: three
    // sentences for four criteria.
    expect(screen.queryAllByText(/^Check /)).toHaveLength(3);
    // And none of the three is an enum's spelling reaching the page.
    expect(document.body.textContent).not.toContain("AcceptanceStatus");
  });

  it("puts the failure's output behind a disclosure rather than on the page", () => {
    render(<AcceptanceSection acceptance={acceptance} log={log} onRunChecks={vi.fn()} />);

    const summary = screen.getByText("Output");
    const details = summary.closest("details");
    expect(details).toBeTruthy();
    // Closed until asked for: `open` absent is what keeps a hundred-line tail off a
    // page somebody opened to read the spec.
    expect(details?.hasAttribute("open")).toBe(false);
    expect(screen.getByText(/F401 imported but unused/)).toBeTruthy();
    // And only the failure has one -- a passing check prints nothing worth a control.
    expect(screen.queryAllByText("Output")).toHaveLength(1);
  });

  it("offers Run checks only when a criterion carries one", () => {
    render(<AcceptanceSection acceptance={acceptance} log={[]} onRunChecks={vi.fn()} />);
    expect(screen.getByRole("button", { name: "Run checks" })).toBeTruthy();

    cleanup();
    render(<AcceptanceSection acceptance={[criterion({ id: "ac-1" })]} log={[]} onRunChecks={vi.fn()} />);
    expect(screen.queryByRole("button", { name: "Run checks" })).toBeNull();
  });

  it("offers no button at all on a surface that passed no handler", () => {
    render(<AcceptanceSection acceptance={acceptance} log={[]} />);
    expect(screen.queryByRole("button", { name: /Run checks/ })).toBeNull();
    // The results are still shown: reading what a check did is not the act that runs one.
    expect(screen.queryAllByText("Check not yet run")).toHaveLength(3);
  });

  it("calls the handler with nothing, and says so while it runs", async () => {
    const onRunChecks = vi.fn(async () => undefined);
    const { rerender } = render(
      <AcceptanceSection acceptance={acceptance} log={[]} onRunChecks={onRunChecks} />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Run checks" }));
    await waitFor(() => expect(onRunChecks).toHaveBeenCalledTimes(1));
    // No argument, because there is nothing a browser may say about what runs.
    expect(onRunChecks).toHaveBeenCalledWith();

    rerender(<AcceptanceSection acceptance={acceptance} log={[]} onRunChecks={onRunChecks} running />);
    const button = screen.getByRole("button", { name: "Running checks…" });
    expect((button as HTMLButtonElement).disabled).toBe(true);
  });

  it("shows the server's own words when it refuses, as an alert", () => {
    render(
      <AcceptanceSection
        acceptance={acceptance}
        log={[]}
        onRunChecks={vi.fn()}
        refusal={{
          reason: "project_not_enabled",
          message: "Project 'agentjobs' is not enabled for dispatch.",
          suggestedAction: "Run 'agentjobs dispatch enable <project>'.",
        }}
      />,
    );

    const alert = screen.getByRole("alert");
    // The server's sentence, not a paraphrase, and its remedy beside it.
    expect(alert.textContent).toContain("Project 'agentjobs' is not enabled for dispatch.");
    expect(alert.textContent).toContain("Run 'agentjobs dispatch enable <project>'.");
    // Rendered under the reason code, which is what makes this the *same* rendering the
    // Dispatch panel gives the same gate rather than a second one that will drift.
    expect(alert.getAttribute("data-refusal-reason")).toBe("project_not_enabled");
  });

  it("never renders a check's argv, so nothing on the page invites editing one", () => {
    render(<AcceptanceSection acceptance={acceptance} log={log} onRunChecks={vi.fn()} />);

    // The commands are on the record and are deliberately not shown: a command in the
    // browser is one step from a field that writes one, and a browser that can write an
    // argv this machine runs is the act task-147 put behind the dispatch gate.
    expect(document.body.textContent).not.toContain("ruff");
    expect(document.body.textContent).not.toContain("pytest");
    expect(document.querySelectorAll("input, textarea, select")).toHaveLength(0);
  });
});
