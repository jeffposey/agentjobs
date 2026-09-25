import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  listIdleSessionsApiSessionsIdleGetOptions,
  listIdleSessionsApiSessionsIdleGetQueryKey,
  updateIdleSessionSettingsApiSessionsIdleSettingsPutMutation,
} from "../api/generated/@tanstack/react-query.gen";
import type {
  IdleSessionEventView,
  IdleSessionView,
  IdleSessionsView,
} from "../api/types";
import { formatElapsed } from "./DispatchPanel";
import {
  ResponsiveCell,
  ResponsiveTable,
  ResponsiveTableRow,
} from "./ResponsiveTable";

/**
 * Idle Claude sessions on this machine, and the sweep that stops them (task-447).
 *
 * Every long-lived Claude Code process shares the machine's login, and each is another
 * refresher in the race that blanks it. The server judges every process; this section
 * shows the verdict and the reason, the switch that lets the poller act on it, and the
 * record of every stop with the command that brings the session back.
 *
 * Re-read once a minute rather than on the live-runs query's two-second clock: an inventory
 * enumerates every process on the machine, and the threshold it judges against is hours.
 */
export const IDLE_SESSIONS_POLL_MS = 60_000;

const KIND_LABELS: Record<string, string> = {
  daemon: "Daemon",
  pty_host: "Terminal host",
  background: "Background session",
  remote_control_host: "Remote Control host",
  remote_control_child: "Remote Control conversation",
  interactive: "Interactive",
  desktop: "Desktop app",
  command: "Command",
};

const VERDICT_LABELS: Record<string, string> = {
  protected: "Never stopped",
  in_use: "In use",
  idle: "Idle",
  report_only: "Idle, reported only",
  candidate: "Would be stopped",
};

export function kindLabel(kind: string): string {
  return KIND_LABELS[kind] ?? kind;
}

export function verdictLabel(verdict: string): string {
  return VERDICT_LABELS[verdict] ?? verdict;
}

/**
 * Desktop helper processes are many and never touched, so they are counted, not listed.
 * A terminal host is its background session's other half and would only repeat its row.
 */
export function listedSessions(body: IdleSessionsView): IdleSessionView[] {
  return body.sessions.filter(
    (session) => session.kind !== "desktop" && session.kind !== "pty_host",
  );
}

export function stopEvents(body: IdleSessionsView): IdleSessionEventView[] {
  return body.events.filter((event) => event.kind === "stop");
}

export function modeSentence(body: IdleSessionsView): string {
  const threshold = formatElapsed(body.settings.idle_minutes * 60);
  if (!body.settings.configured) {
    return "Report only. This machine has no dispatch config to switch enforcement on in.";
  }
  if (body.settings.enforce) {
    return `Enforcing: idle, resumable sessions quiet for ${threshold} are stopped automatically.`;
  }
  return `Report only: nothing is stopped. With enforcement on, sessions quiet for ${threshold} would be.`;
}

function VerdictBadge({ verdict }: { verdict: string }) {
  const tone =
    verdict === "candidate"
      ? "border-amber-500/40 bg-amber-500/10 text-amber-300"
      : verdict === "in_use"
        ? "border-green-500/40 bg-green-500/10 text-green-300"
        : "border-dark-border bg-dark-bg text-dark-muted";
  return (
    <span
      className={`inline-block rounded border px-2 py-0.5 text-xs ${tone}`}
      data-verdict={verdict}
    >
      {verdictLabel(verdict)}
    </span>
  );
}

function localTime(iso: string | null | undefined): string {
  if (!iso) return "unknown";
  const parsed = new Date(iso);
  return Number.isNaN(parsed.getTime()) ? iso : parsed.toLocaleString();
}

export function IdleSessionsPanel({
  body,
  onSetEnforce,
  pending = false,
  error = null,
}: {
  body: IdleSessionsView | null;
  onSetEnforce?: (enforce: boolean) => void;
  pending?: boolean;
  error?: string | null;
}) {
  const [confirming, setConfirming] = useState(false);

  if (!body) {
    return (
      <section className="rounded-lg border border-dark-border bg-dark-surface p-6">
        <h2 className="text-lg font-semibold">Idle Claude sessions</h2>
        <p className="mt-2 text-sm text-dark-muted">
          Reading this machine&apos;s processes…
        </p>
      </section>
    );
  }

  const listed = listedSessions(body);
  const desktop = body.sessions.filter((session) => session.kind === "desktop").length;
  const stops = stopEvents(body);
  const modes = body.events.filter((event) => event.kind === "mode");
  const enforce = body.settings.enforce;

  return (
    <section
      className="rounded-lg border border-dark-border bg-dark-surface"
      data-testid="idle-sessions"
    >
      <div className="space-y-3 border-b border-dark-border p-6">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <h2 className="text-lg font-semibold">Idle Claude sessions</h2>
          <p className="text-sm text-dark-muted" data-testid="idle-mode">
            {modeSentence(body)}
          </p>
        </div>
        <p className="text-xs text-dark-muted">
          Every Claude process sharing this machine&apos;s login refreshes it,
          and two refreshing at once can log every session out. Remote Control
          hosts, the daemon, the desktop app, busy or attached sessions and
          AgentJobs runs are never stopped. A stopped session keeps its
          conversation and can be resumed.
        </p>
        {body.settings.configured && onSetEnforce && (
          <div className="flex flex-wrap items-center gap-2">
            {!confirming ? (
              <button
                type="button"
                className="rounded border border-dark-border px-3 py-1 text-sm hover:bg-dark-bg"
                disabled={pending}
                onClick={() =>
                  enforce ? onSetEnforce(false) : setConfirming(true)
                }
              >
                {enforce ? "Turn enforcement off" : "Turn enforcement on…"}
              </button>
            ) : (
              <>
                <span className="text-sm">
                  Stop idle sessions automatically from now on?
                </span>
                <button
                  type="button"
                  className="rounded border border-amber-500/60 px-3 py-1 text-sm text-amber-300 hover:bg-amber-500/10"
                  disabled={pending}
                  onClick={() => {
                    setConfirming(false);
                    onSetEnforce(true);
                  }}
                >
                  Turn on
                </button>
                <button
                  type="button"
                  className="rounded border border-dark-border px-3 py-1 text-sm hover:bg-dark-bg"
                  onClick={() => setConfirming(false)}
                >
                  Cancel
                </button>
              </>
            )}
          </div>
        )}
        {error && <p className="text-sm text-red-400">{error}</p>}
        {(body.errors ?? []).map((message) => (
          <p key={message} className="text-sm text-amber-300">
            {message}
          </p>
        ))}
      </div>

      <div className="p-2">
        <ResponsiveTable
          aria-label="Claude processes"
          columns={[null, "11rem", "10rem", "7rem"]}
        >
          <thead>
            <tr>
              <th scope="col">Process</th>
              <th scope="col">Kind</th>
              <th scope="col">Verdict</th>
              <th scope="col">Quiet for</th>
            </tr>
          </thead>
          <tbody>
            {listed.map((session) => (
              <ResponsiveTableRow key={session.pid} data-pid={session.pid}>
                <ResponsiveCell label="Process">
                  <div className="text-sm">
                    {session.name || session.short_id || `pid ${session.pid}`}
                    {session.short_id && session.name && (
                      <span className="ml-2 font-mono text-xs text-dark-muted">
                        {session.short_id}
                      </span>
                    )}
                  </div>
                  <div className="text-xs text-dark-muted">
                    {session.reason}
                  </div>
                </ResponsiveCell>
                <ResponsiveCell
                  label="Kind"
                  className="text-sm text-dark-muted"
                >
                  {kindLabel(session.kind)}
                </ResponsiveCell>
                <ResponsiveCell label="Verdict">
                  <VerdictBadge verdict={session.verdict} />
                </ResponsiveCell>
                <ResponsiveCell
                  label="Quiet for"
                  className="text-sm text-dark-muted"
                >
                  {session.idle_seconds === null ||
                  session.idle_seconds === undefined
                    ? "—"
                    : formatElapsed(session.idle_seconds)}
                </ResponsiveCell>
              </ResponsiveTableRow>
            ))}
          </tbody>
        </ResponsiveTable>
        {desktop > 0 && (
          <p
            className="px-4 pb-2 text-xs text-dark-muted"
            data-testid="desktop-count"
          >
            Plus {desktop} Claude desktop app processes, which use their own
            login and are never touched.
          </p>
        )}
      </div>

      <div className="border-t border-dark-border p-6">
        <h3 className="text-base font-semibold">Stopped by the sweep</h3>
        {stops.length === 0 ? (
          <p className="mt-2 text-sm text-dark-muted" data-testid="no-stops">
            Nothing has been stopped.
          </p>
        ) : (
          <ul className="mt-2 divide-y divide-dark-border">
            {stops.map((event) => (
              <li
                key={event.event_id}
                className="space-y-1 py-3"
                data-event-id={event.event_id}
              >
                <div className="flex flex-wrap items-baseline gap-2 text-sm">
                  <span className="font-medium">
                    {event.name || event.short_id}
                  </span>
                  <span className="text-dark-muted">{event.outcome}</span>
                  <span className="text-xs text-dark-muted">
                    {localTime(event.at)}
                  </span>
                </div>
                <div className="text-xs text-dark-muted">
                  {event.cwd} · last active {localTime(event.last_activity)}
                </div>
                <div className="text-xs text-dark-muted">
                  {event.outcome === "stopped" ? event.reason : event.detail}
                </div>
                {event.outcome === "stopped" &&
                  (event.resume_commands ?? []).map((command) => (
                    <code
                      key={command}
                      className="block font-mono text-xs text-dark-text"
                    >
                      {command}
                    </code>
                  ))}
              </li>
            ))}
          </ul>
        )}
        {modes.length > 0 && (
          <ul className="mt-3 space-y-1 text-xs text-dark-muted">
            {modes.map((event) => (
              <li key={event.event_id}>
                Enforcement{" "}
                {event.outcome === "enforce" ? "switched on" : "switched off"}{" "}
                at {localTime(event.at)}
                {event.auth_incidents !== null &&
                event.auth_incidents !== undefined
                  ? ` (auth incidents recorded then: ${event.auth_incidents})`
                  : ""}
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}

/** The section as the Dispatch settings page mounts it (task-588): its own query, and the switch. */
export function IdleSessionsSection() {
  const queryClient = useQueryClient();
  const query = useQuery({
    ...listIdleSessionsApiSessionsIdleGetOptions(),
    refetchInterval: IDLE_SESSIONS_POLL_MS,
  });
  const update = useMutation({
    ...updateIdleSessionSettingsApiSessionsIdleSettingsPutMutation(),
    onSuccess: () =>
      queryClient.invalidateQueries({
        queryKey: listIdleSessionsApiSessionsIdleGetQueryKey(),
      }),
  });
  return (
    <IdleSessionsPanel
      body={query.data ?? null}
      pending={update.isPending}
      error={update.error ? "The setting could not be saved." : null}
      onSetEnforce={(enforce) => update.mutate({ body: { enforce } })}
    />
  );
}
