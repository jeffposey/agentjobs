import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import type { EmergencyStopItem } from "../api/generated";
import {
  getEmergencyStopApiRunsEmergencyStopGetOptions,
  getEmergencyStopApiRunsEmergencyStopGetQueryKey,
  pressEmergencyStopApiRunsEmergencyStopPostMutation,
  resumeAfterEmergencyStopApiRunsEmergencyStopResumePostMutation,
} from "../api/generated/@tanstack/react-query.gen";
import { readRefusal } from "../api/mutation-error";

/**
 * The machine-wide emergency stop (task-573), immediately left of the capture trigger.
 *
 * **One press and one confirm, with nothing to type.** The confirm is there because a
 * mis-tap kills in-flight work, which is not free. Nothing more than that, because a
 * kill switch that asks questions is not one: the route takes no body, for the reason
 * `disable_dispatch` gives.
 *
 * **While the sentinel is down, the state is a strip under the bar, not a wider button.**
 * The trigger turns solid red and {@link StoppedBanner} spans the header beneath the nav
 * saying so, with Resume on it. So nobody can be on a screen that looks normal while
 * every dispatch is refused, which was true everywhere before this. A "Stopped" text
 * pill in the row was built first and measured: it cost 57px, which pushed
 * `NAV_INLINE_MIN_PX` up by that much again and squeezed the project switcher to zero
 * width on a 375px phone. The strip costs the row nothing.
 *
 * **Polled on its own slow clock.** The answer is a file's existence, is machine-wide and
 * is not a task write, so no project's revision would move when it changes. Thirty
 * seconds is how long a second device can go without seeing a stop pressed elsewhere.
 * Pressing or resuming here updates this device at once.
 */

export const EMERGENCY_STOP_POLL_MS = 30_000;

/** A stop square in an octagon, drawn to match the plus and the kebab in weight and box. */
function StopIcon() {
  return (
    <svg width="20" height="20" viewBox="0 0 20 20" aria-hidden="true" focusable="false">
      <path
        d="M7 2.5h6l4.5 4.5v6L13 17.5H7L2.5 13V7z"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.75"
        strokeLinejoin="round"
      />
      <rect x="7.5" y="7.5" width="5" height="5" rx="0.5" fill="currentColor" />
    </svg>
  );
}

const KIND_WORDS: Record<string, string> = {
  run: "Run",
  walk: "Epic walk",
  pull: "Pull mode",
  queue: "Queued dispatch",
};

/** The machine's stop state, shared by the trigger and the strip. */
function useEmergencyStopState() {
  return useQuery({
    ...getEmergencyStopApiRunsEmergencyStopGetOptions(),
    refetchInterval: EMERGENCY_STOP_POLL_MS,
  });
}

export function EmergencyStop({ className = "" }: { className?: string }) {
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const state = useEmergencyStopState();
  const stopped = state.data?.stopped ?? false;

  const close = () => {
    setOpen(false);
    triggerRef.current?.focus();
  };

  return (
    <div className={`relative shrink-0 ${className}`}>
      <button
        ref={triggerRef}
        type="button"
        onClick={() => setOpen(true)}
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-label={stopped ? "Dispatch stopped — resume" : "Emergency stop"}
        title={
          stopped
            ? state.data?.note || "Dispatch is stopped machine-wide"
            : "Emergency stop: stop everything AgentJobs started"
        }
        data-testid="emergency-stop"
        data-stopped={stopped ? "true" : "false"}
        // Red at rest, unlike the plus and the kebab beside it. Blue in this bar means
        // "you are here" (task-336); red means only this, and a panic button has to be
        // findable without reading the header. Solid once pressed, the same 44px box.
        className={
          stopped
            ? "touch-target rounded-md bg-red-600 px-3 text-white hover:bg-red-500 focus:outline-none focus:ring-2 focus:ring-red-300"
            : "touch-target rounded-md px-3 text-red-400 hover:bg-red-950 hover:text-red-300 focus:outline-none focus:ring-1 focus:ring-red-400"
        }
      >
        <StopIcon />
      </button>
      {open &&
        createPortal(
          <EmergencyStopDialog stopped={stopped} note={state.data?.note ?? ""} onClose={close} />,
          document.body,
        )}
    </div>
  );
}

/**
 * The stopped state, across the whole header, on every page that has one.
 *
 * Nothing when dispatch is running, so it costs an ordinary screen no height at all.
 */
export function StoppedBanner() {
  const [open, setOpen] = useState(false);
  const resumeRef = useRef<HTMLButtonElement>(null);
  const state = useEmergencyStopState();
  if (!state.data?.stopped) return null;

  const close = () => {
    setOpen(false);
    resumeRef.current?.focus();
  };

  return (
    <div
      role="status"
      data-testid="stopped-banner"
      className="border-t border-red-800 bg-red-700 text-white"
    >
      <div className="mx-auto flex max-w-7xl items-center gap-3 px-4 py-1.5 text-sm sm:px-6 lg:px-8">
        <span className="min-w-0 flex-1">
          <strong>Dispatch stopped.</strong> Nothing new will start on this machine.
        </span>
        <button
          ref={resumeRef}
          type="button"
          onClick={() => setOpen(true)}
          aria-haspopup="dialog"
          className="shrink-0 rounded-md border border-white/70 px-3 py-1 font-semibold hover:bg-red-600 focus:outline-none focus:ring-2 focus:ring-white"
        >
          Resume…
        </button>
      </div>
      {open &&
        createPortal(
          <EmergencyStopDialog stopped note={state.data?.note ?? ""} onClose={close} />,
          document.body,
        )}
    </div>
  );
}

function EmergencyStopDialog({
  stopped,
  note,
  onClose,
}: {
  stopped: boolean;
  note: string;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const [items, setItems] = useState<Array<EmergencyStopItem> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const press = useMutation(pressEmergencyStopApiRunsEmergencyStopPostMutation());
  const resume = useMutation(resumeAfterEmergencyStopApiRunsEmergencyStopResumePostMutation());
  const busy = press.isPending || resume.isPending;

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !busy) onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [busy, onClose]);

  const settle = (data: unknown) => {
    queryClient.setQueryData(getEmergencyStopApiRunsEmergencyStopGetQueryKey(), data as never);
    // Everything else on the page may have changed under it: runs, walks, the queue.
    void queryClient.invalidateQueries();
  };

  const fail = (caught: unknown, fallback: string) => {
    const refusal = readRefusal(caught);
    setError(refusal ? refusal.message : fallback);
  };

  const doStop = () => {
    setError(null);
    press.mutate(
      {},
      {
        onSuccess: (data) => {
          setItems(data.items ?? []);
          settle(data);
        },
        onError: (caught) =>
          fail(caught, "The stop could not be sent. Check the server, then press it again."),
      },
    );
  };

  const doResume = () => {
    setError(null);
    resume.mutate(
      {},
      {
        onSuccess: (data) => {
          settle(data);
          onClose();
        },
        onError: (caught) =>
          fail(caught, "Resume could not be sent. Check the server, then press it again."),
      },
    );
  };

  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-black/60 p-4 sm:items-center">
      <section
        role="dialog"
        aria-modal="true"
        aria-labelledby="emergency-stop-heading"
        className="max-h-full w-full max-w-lg overflow-y-auto rounded-2xl border border-red-800 bg-dark-surface p-5"
      >
        {items !== null ? (
          <StopReport items={items} note={note} onClose={onClose} />
        ) : stopped ? (
          <>
            <h2 id="emergency-stop-heading" className="text-2xl font-bold text-red-400">
              Dispatch is stopped
            </h2>
            <p className="mt-3 text-dark-text">
              Every dispatch on this machine is refused until someone resumes it.
            </p>
            {note && <p className="mt-2 text-sm text-dark-muted">{note}</p>}
            <p className="mt-3 text-sm text-dark-muted">
              Resuming starts nothing. Runs, walks, the queue and pull mode that the stop ended
              stay ended, so dispatch again what you want running.
            </p>
            {error && (
              <p role="alert" className="mt-3 text-sm text-red-400">
                {error}
              </p>
            )}
            <div className="mt-5 flex justify-end gap-2">
              <button
                type="button"
                onClick={onClose}
                disabled={busy}
                className="touch-target rounded-md px-4 text-dark-text hover:bg-dark-border"
              >
                Close
              </button>
              <button
                type="button"
                onClick={doResume}
                disabled={busy}
                className="touch-target rounded-md bg-blue-600 px-4 font-semibold text-white hover:bg-blue-500 disabled:opacity-60"
              >
                {resume.isPending ? "Resuming…" : "Resume dispatch"}
              </button>
            </div>
          </>
        ) : (
          <>
            <h2 id="emergency-stop-heading" className="text-2xl font-bold text-red-400">
              Stop everything?
            </h2>
            <p className="mt-3 text-dark-text">
              This stops every run AgentJobs started on this machine and refuses new ones.
            </p>
            <ul className="mt-2 list-disc pl-5 text-sm text-dark-muted">
              <li>Live runs are killed. Work they had not committed is lost.</li>
              <li>Epic walks, pull mode and queued dispatches are ended, not paused.</li>
              <li>A finish already gating merges nothing.</li>
              <li>Your own interactive sessions are left running.</li>
            </ul>
            {error && (
              <p role="alert" className="mt-3 text-sm text-red-400">
                {error}
              </p>
            )}
            <div className="mt-5 flex justify-end gap-2">
              <button
                type="button"
                onClick={onClose}
                disabled={busy}
                className="touch-target rounded-md px-4 text-dark-text hover:bg-dark-border"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={doStop}
                disabled={busy}
                className="touch-target rounded-md bg-red-600 px-4 font-semibold text-white hover:bg-red-500 disabled:opacity-60"
              >
                {press.isPending ? "Stopping…" : "Stop everything"}
              </button>
            </div>
          </>
        )}
      </section>
    </div>
  );
}

function StopReport({
  items,
  note,
  onClose,
}: {
  items: Array<EmergencyStopItem>;
  note: string;
  onClose: () => void;
}) {
  return (
    <>
      <h2 id="emergency-stop-heading" className="text-2xl font-bold text-red-400">
        Dispatch stopped
      </h2>
      {note && <p className="mt-2 text-sm text-dark-muted">{note}</p>}
      {items.length === 0 ? (
        <p className="mt-3 text-dark-text">Nothing was running, queued, armed or walking.</p>
      ) : (
        <ul className="mt-3 space-y-2" data-testid="emergency-stop-results">
          {items.map((item) => (
            <li
              key={`${item.kind}:${item.id}`}
              className="rounded-md border border-dark-border px-3 py-2 text-sm"
            >
              <div className="flex items-baseline justify-between gap-2">
                <span className="font-semibold">{KIND_WORDS[item.kind] ?? item.kind}</span>
                <span className={item.stopped ? "text-dark-muted" : "font-semibold text-red-400"}>
                  {item.stopped ? "stopped" : "not confirmed"}
                </span>
              </div>
              <div className="break-all font-mono text-xs text-dark-muted">{item.id}</div>
              <div className="mt-1 text-dark-text">{item.detail}</div>
            </li>
          ))}
        </ul>
      )}
      <div className="mt-5 flex justify-end">
        <button
          type="button"
          onClick={onClose}
          className="touch-target rounded-md px-4 text-dark-text hover:bg-dark-border"
        >
          Close
        </button>
      </div>
    </>
  );
}
