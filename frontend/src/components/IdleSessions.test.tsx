import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { IdleSessionEventView, IdleSessionView, IdleSessionsView } from "../api/types";
import { IdleSessionsPanel, listedSessions, modeSentence } from "./IdleSessions";

/**
 * The idle-session section (task-447), rendered from a response object. The server makes
 * every judgement; what this file owns is that the reader sees each verdict with its reason,
 * that turning enforcement on takes a second click, and that a stop shows how to resume.
 */

function session(overrides: Partial<IdleSessionView> = {}): IdleSessionView {
  return {
    pid: 12,
    kind: "background",
    verdict: "candidate",
    reason: "Ledger says idle and the transcript has been quiet for 5h 00m.",
    session_id: "764ca346-1111-4222-8333-444455556666",
    short_id: "764ca346",
    name: "durable execution design",
    cwd: "C:/projects/agentjobs",
    ledger_status: "idle",
    run_id: null,
    last_activity: "2026-09-13T18:00:00Z",
    idle_seconds: 18_000,
    resume_commands: ["claude attach 764ca346"],
    ...overrides,
  };
}

function stop(overrides: Partial<IdleSessionEventView> = {}): IdleSessionEventView {
  return {
    event_id: "ise_1",
    kind: "stop",
    at: "2026-09-13T23:00:00Z",
    session_id: "764ca346-1111-4222-8333-444455556666",
    outcome: "stopped",
    detail: "stopped",
    trigger: "sweep",
    name: "durable execution design",
    cwd: "C:/projects/agentjobs",
    short_id: "764ca346",
    last_activity: "2026-09-13T18:00:00Z",
    idle_seconds: 18_000,
    reason: "quiet for 5h 00m",
    resume_commands: [
      "claude attach 764ca346",
      "claude --bg --resume 764ca346-1111-4222-8333-444455556666",
    ],
    auth_incidents: null,
    ...overrides,
  };
}

function body(overrides: Partial<IdleSessionsView> = {}): IdleSessionsView {
  return {
    settings: {
      configured: true,
      enforce: false,
      idle_minutes: 240,
      max_stops_per_sweep: 3,
      sweep_interval_seconds: 300,
    },
    generated_at: "2026-09-13T23:30:00Z",
    sessions: [
      session(),
      session({
        pid: 20,
        kind: "remote_control_host",
        verdict: "protected",
        reason: "A Remote Control host. The owner relies on Remote Control.",
        name: "",
        short_id: null,
        idle_seconds: null,
      }),
      session({ pid: 30, kind: "desktop", verdict: "protected", name: "" }),
    ],
    errors: [],
    events: [],
    auth_incidents: 0,
    ...overrides,
  };
}

describe("IdleSessionsPanel", () => {
  it("shows each process with its verdict and the reason for it", () => {
    render(<IdleSessionsPanel body={body()} />);

    const table = screen.getByRole("table", { name: "Claude processes" });
    expect(within(table).getByText("Would be stopped")).toBeTruthy();
    expect(within(table).getByText(/quiet for 5h 00m/)).toBeTruthy();
    expect(within(table).getByText("Remote Control host")).toBeTruthy();
    expect(within(table).getByText(/owner relies on Remote Control/)).toBeTruthy();
  });

  it("counts desktop processes instead of listing them", () => {
    expect(listedSessions(body()).map((s) => s.pid)).toEqual([12, 20]);
    render(<IdleSessionsPanel body={body()} />);
    expect(screen.getByTestId("desktop-count").textContent).toContain("1 Claude desktop");
  });

  it("says report mode stops nothing", () => {
    expect(modeSentence(body())).toMatch(/^Report only: nothing is stopped/);
  });

  it("asks for a second click before turning enforcement on", () => {
    const onSetEnforce = vi.fn();
    render(<IdleSessionsPanel body={body()} onSetEnforce={onSetEnforce} />);

    fireEvent.click(screen.getByRole("button", { name: "Turn enforcement on…" }));
    expect(onSetEnforce).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Turn on" }));

    expect(onSetEnforce).toHaveBeenCalledWith(true);
  });

  it("turns enforcement off in one click", () => {
    const onSetEnforce = vi.fn();
    const enforcing = body({ settings: { ...body().settings, enforce: true } });
    render(<IdleSessionsPanel body={enforcing} onSetEnforce={onSetEnforce} />);

    fireEvent.click(screen.getByRole("button", { name: "Turn enforcement off" }));

    expect(onSetEnforce).toHaveBeenCalledWith(false);
  });

  it("records a stop with where it was, when it was last active and how to resume it", () => {
    render(<IdleSessionsPanel body={body({ events: [stop()] })} />);

    const item = document.querySelector('[data-event-id="ise_1"]') as HTMLElement;
    expect(within(item).getByText("durable execution design")).toBeTruthy();
    expect(within(item).getByText(/C:\/projects\/agentjobs/)).toBeTruthy();
    expect(within(item).getByText("claude attach 764ca346")).toBeTruthy();
    expect(
      within(item).getByText("claude --bg --resume 764ca346-1111-4222-8333-444455556666"),
    ).toBeTruthy();
  });

  it("shows the auth incident count at a switch-over", () => {
    const mode = stop({ event_id: "ise_2", kind: "mode", outcome: "enforce", auth_incidents: 3 });
    render(<IdleSessionsPanel body={body({ events: [mode] })} />);
    expect(screen.getByText(/switched on/).textContent).toContain("auth incidents recorded then: 3");
  });
});
