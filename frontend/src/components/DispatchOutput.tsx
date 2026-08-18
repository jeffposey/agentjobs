import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { readDispatchRunTailApiProjectsProjectIdDispatchRunsRunIdTailGetOptions } from "../api/generated/@tanstack/react-query.gen";
import type { DispatchRunView } from "../api/types";

/**
 * A dispatched run's output, on the task page, while it is still happening.
 *
 * The surface this replaces was a "View output" link to another tab, which meant that
 * for the whole of a run -- minutes, sometimes many -- the only thing a watcher could
 * see was an elapsed counter going up. Watching a run is the normal case, not the
 * exception, so the output belongs on the page the reader is already on.
 *
 * **One section whose contents change, not two surfaces.** The same panel tails a live
 * run and holds the finished output afterwards. A transition between two different
 * places at the moment a run ends is exactly when a reader is looking at it.
 */

export const TAIL_POLL_MS = 10_000;
/**
 * How often a watching browser re-reads the tail.
 *
 * Deliberately the same as the server's `SESSION_POLL_SECONDS`, and never faster. The
 * text comes from a file the session poller writes, so polling more often would return
 * the same bytes -- and the reason to be careful here is that the *source* of those
 * bytes is a subprocess. A per-browser clock spawning `claude logs` would put the cost
 * of watching on the machine running the work.
 */

/** Poll while something is live; a finished run's output cannot change. */
export function tailPollInterval(live: boolean): number | false {
  return live ? TAIL_POLL_MS : false;
}

/** What to say when there is no text yet -- which is different from there being none. */
export function emptyOutputNote(live: boolean, source: string): string {
  if (source !== "none") return "";
  return live
    ? "Nothing captured yet. A session's output is copied across each time the run is polled, so the first lines appear within a few seconds of the session writing them."
    : "This run captured no output.";
}

/** Where the text came from, in words, so nobody wonders what they are reading. */
export function sourceNote(source: string): string {
  switch (source) {
    case "session-transcript":
      return "The session's own transcript.";
    case "captured-output":
      return "What the run wrote to stdout and stderr.";
    default:
      return "";
  }
}

export function DispatchRunOutput({ run }: { run: DispatchRunView }) {
  // Expanded while the run is live, and it stays expanded when that run finishes: a
  // panel that collapses itself at the moment a run ends hides the output at the one
  // moment the person watching wanted it. Runs that were already over when the page
  // loaded start collapsed, because a task with several old runs is otherwise a wall of
  // terminal output with the task record somewhere underneath it.
  const [open, setOpen] = useState(run.live);
  const scroller = useRef<HTMLPreElement | null>(null);

  const tail = useQuery({
    ...readDispatchRunTailApiProjectsProjectIdDispatchRunsRunIdTailGetOptions({
      path: { project_id: run.project_id, run_id: run.run_id },
    }),
    enabled: open,
    refetchInterval: () => tailPollInterval(run.live),
  });

  const text = tail.data?.text ?? "";
  useEffect(() => {
    // A tail that does not follow is a paged reader. Only while live: scrolling a
    // finished run to the bottom would fight anyone reading it from the top.
    if (open && run.live && scroller.current) {
      scroller.current.scrollTop = scroller.current.scrollHeight;
    }
  }, [open, run.live, text]);

  const panelId = `dispatch-output-${run.run_id}`;
  const empty = emptyOutputNote(run.live, tail.data?.source ?? "none");

  return (
    <div className="mt-2" data-testid={`dispatch-output-${run.run_id}`}>
      <button
        type="button"
        aria-expanded={open}
        aria-controls={panelId}
        onClick={() => setOpen((current) => !current)}
        className="flex items-center gap-2 text-sm font-semibold text-blue-300 hover:text-blue-200"
      >
        <span aria-hidden="true">{open ? "▾" : "▸"}</span>
        Output
        {run.live && (
          <span className="rounded bg-sky-900 px-2 py-0.5 text-xs font-semibold text-sky-200">
            live
          </span>
        )}
      </button>

      {open && (
        <div id={panelId} className="mt-2 rounded-lg border border-dark-border bg-dark-bg p-3">
          {tail.isPending && <p className="text-sm text-dark-muted">Reading this run's output…</p>}
          {tail.isError && (
            <p role="status" className="text-sm text-orange-200">
              This run's output could not be read just now. It is still on disk; the next
              poll will pick it up.
            </p>
          )}
          {tail.data && empty && <p className="text-sm text-dark-muted">{empty}</p>}
          {tail.data && !empty && (
            <pre
              ref={scroller}
              data-run-output={run.run_id}
              className="max-h-80 overflow-auto whitespace-pre-wrap break-words text-xs leading-relaxed text-dark-text"
            >
              {text}
            </pre>
          )}
          {/* No "open the full output" link here on purpose: the run's own row carries
              one, two feet above this panel, and a second copy of it inside the panel is
              just a second thing to read. */}
          {tail.data && !empty && (
            <p className="mt-2 text-xs text-dark-muted">
              {sourceNote(tail.data.source)} Last {tail.data.lines} lines.
            </p>
          )}
        </div>
      )}
    </div>
  );
}
