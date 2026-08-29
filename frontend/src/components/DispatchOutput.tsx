import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import {
  readDispatchRunTailApiProjectsProjectIdDispatchRunsRunIdTailGetOptions,
  readDispatchRunTranscriptApiProjectsProjectIdDispatchRunsRunIdTranscriptGetOptions,
} from "../api/generated/@tanstack/react-query.gen";
import type { DispatchRunView, TranscriptCallView, TranscriptEntryView } from "../api/types";

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
 *
 * **Two readings of one run, and the readable one leads.** What this used to show was
 * the session's terminal, and a terminal is a *repaint*: a TUI draws a space by moving
 * the cursor rather than by emitting one, so an approval dialog arrived as
 * `NewMCPserverfoundinthisproject:agentjobs`. The readable view renders the structured
 * transcript the session writes about itself instead -- prose, and runs of tool calls
 * collapsed into a line you can open. Raw is still one click away, because when a
 * session dies in a way no renderer models the unparsed bytes are the only evidence
 * there is.
 */

export const TAIL_POLL_MS = 10_000;
/**
 * How often a watching browser re-reads the output.
 *
 * Deliberately the same as the server's `SESSION_POLL_SECONDS`, and never faster. The
 * raw text comes from a file the session poller writes, so polling more often would
 * return the same bytes -- and the reason to be careful here is that the *source* of
 * those bytes is a subprocess. A per-browser clock spawning `claude logs` would put the
 * cost of watching on the machine running the work.
 *
 * The structured transcript is written by the session itself and so is fresher, but it
 * is polled on the same clock: a second interval would be a second thing to reason
 * about, for a panel a human reads at human speed.
 */

export const TRANSCRIPT_ENTRIES = 40;
/** How many entries the readable view asks for. An entry is one thing the agent did. */

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

/** Lines added and removed by a run of calls, or nothing when none were. */
export function diffLabel(added: number, removed: number): string {
  if (!added && !removed) return "";
  return `+${added} -${removed}`;
}

/**
 * How many calls in a run of them failed.
 *
 * Rendered as words rather than as colour alone: a run that is quietly erroring looked
 * exactly like one that is working, and a reader scanning for trouble should be able to
 * find it by reading rather than by noticing a hue.
 */
export function failureLabel(failed: number): string {
  if (failed <= 0) return "";
  return failed === 1 ? "1 failed" : `${failed} failed`;
}

/** Whether the readable view has nothing structured behind it and must degrade. */
export function transcriptUnavailable(source: string | undefined): boolean {
  return source !== "session-jsonl";
}

function CallDetail({ call }: { call: TranscriptCallView }) {
  const counts = diffLabel(call.added ?? 0, call.removed ?? 0);
  return (
    <li className="border-l-2 border-dark-border pl-3">
      <p className="flex flex-wrap items-baseline gap-2 text-xs">
        <span className="font-semibold text-dark-text">{call.name}</span>
        {call.title && <span className="text-dark-muted">{call.title}</span>}
        {counts && <span className="text-emerald-300">{counts}</span>}
        {call.failed && (
          <span className="rounded bg-red-950 px-1.5 py-0.5 font-semibold text-red-200">
            failed
          </span>
        )}
      </p>
      {call.detail && call.detail !== call.title && (
        <pre className="mt-1 overflow-x-auto whitespace-pre-wrap break-words text-xs text-dark-muted">
          {call.detail}
        </pre>
      )}
      {call.output && (
        <pre
          className={`mt-1 max-h-40 overflow-auto whitespace-pre-wrap break-words text-xs ${
            call.failed ? "text-red-200" : "text-dark-muted"
          }`}
        >
          {call.output}
        </pre>
      )}
    </li>
  );
}

/**
 * A run of consecutive tool calls: one summary line, with the detail behind a
 * disclosure.
 *
 * Collapsed by default because the summary is what makes the panel scannable. The
 * defect being fixed is a wall of everything, and re-dumping every call by default
 * would reproduce it with nicer spacing.
 */
function ToolGroup({ entry }: { entry: TranscriptEntryView }) {
  const [open, setOpen] = useState(false);
  const calls = entry.calls ?? [];
  const counts = diffLabel(entry.added ?? 0, entry.removed ?? 0);
  const failures = failureLabel(entry.failed ?? 0);

  return (
    <div className="rounded border border-dark-border/60 px-3 py-2">
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((current) => !current)}
        className="flex w-full flex-wrap items-baseline gap-2 text-left text-sm text-dark-text hover:text-blue-200"
      >
        <span aria-hidden="true" className="text-dark-muted">
          {open ? "▾" : "▸"}
        </span>
        <span className="font-medium">{entry.summary}</span>
        {counts && <span className="text-xs text-emerald-300">{counts}</span>}
        {failures && (
          <span className="rounded bg-red-950 px-1.5 py-0.5 text-xs font-semibold text-red-200">
            {failures}
          </span>
        )}
      </button>
      {open && (
        <ul className="mt-2 space-y-2">
          {calls.map((call, index) => (
            <CallDetail key={index} call={call} />
          ))}
        </ul>
      )}
    </div>
  );
}

function Entry({ entry }: { entry: TranscriptEntryView }) {
  if (entry.kind === "tools") return <ToolGroup entry={entry} />;
  if (entry.kind === "prompt") {
    return (
      <div className="rounded border border-dark-border/60 px-3 py-2">
        <p className="text-xs font-semibold uppercase tracking-wide text-dark-muted">Asked</p>
        <p className="mt-1 whitespace-pre-wrap break-words text-sm text-dark-muted">{entry.text}</p>
      </div>
    );
  }
  return (
    <p className="whitespace-pre-wrap break-words px-1 text-sm leading-relaxed text-dark-text">
      {entry.text}
    </p>
  );
}

export function DispatchRunOutput({ run }: { run: DispatchRunView }) {
  // Expanded while the run is live, and it stays expanded when that run finishes: a
  // panel that collapses itself at the moment a run ends hides the output at the one
  // moment the person watching wanted it. Runs that were already over when the page
  // loaded start collapsed, because a task with several old runs is otherwise a wall of
  // terminal output with the task record somewhere underneath it.
  const [open, setOpen] = useState(run.live);
  const [raw, setRaw] = useState(false);
  const scroller = useRef<HTMLDivElement | null>(null);

  const transcript = useQuery({
    ...readDispatchRunTranscriptApiProjectsProjectIdDispatchRunsRunIdTranscriptGetOptions({
      path: { project_id: run.project_id, run_id: run.run_id },
      query: { entries: TRANSCRIPT_ENTRIES },
    }),
    enabled: open && !raw,
    refetchInterval: () => tailPollInterval(run.live),
  });

  // The raw capture is fetched for the raw view, and also whenever the readable one has
  // nothing to render -- a batch run, a session that has not reported its id yet, a
  // driver that keeps no structured transcript. Degrading to the bytes is the whole
  // reason the raw view was kept.
  const degraded = !raw && transcript.isSuccess && transcriptUnavailable(transcript.data.source);
  const tail = useQuery({
    ...readDispatchRunTailApiProjectsProjectIdDispatchRunsRunIdTailGetOptions({
      path: { project_id: run.project_id, run_id: run.run_id },
    }),
    enabled: open && (raw || degraded),
    refetchInterval: () => tailPollInterval(run.live),
  });

  const text = tail.data?.text ?? "";
  const entries = transcript.data?.entries ?? [];
  useEffect(() => {
    // A tail that does not follow is a paged reader. Only while live: scrolling a
    // finished run to the bottom would fight anyone reading it from the top.
    if (open && run.live && scroller.current) {
      scroller.current.scrollTop = scroller.current.scrollHeight;
    }
  }, [open, run.live, text, entries.length]);

  const panelId = `dispatch-output-${run.run_id}`;
  const empty = emptyOutputNote(run.live, tail.data?.source ?? "none");
  const showRawText = raw || degraded;

  return (
    <div className="mt-2" data-testid={`dispatch-output-${run.run_id}`}>
      <div className="flex flex-wrap items-center gap-3">
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
          <button
            type="button"
            aria-pressed={raw}
            onClick={() => setRaw((current) => !current)}
            className="rounded border border-dark-border px-2 py-0.5 text-xs text-dark-muted hover:text-dark-text"
          >
            {raw ? "Show readable" : "Show raw terminal"}
          </button>
        )}
      </div>

      {open && (
        <div id={panelId} className="mt-2 rounded-lg border border-dark-border bg-dark-bg p-3">
          {!raw && transcript.isPending && (
            <p className="text-sm text-dark-muted">Reading this run's transcript…</p>
          )}
          {!raw && transcript.isError && (
            <p role="status" className="text-sm text-orange-200">
              This run's transcript could not be read just now. It is still on disk; the next
              poll will pick it up.
            </p>
          )}
          {degraded && transcript.data?.note && (
            <p className="mb-2 text-sm text-dark-muted">{transcript.data.note}</p>
          )}

          {!showRawText && entries.length > 0 && (
            <div ref={scroller} className="max-h-96 space-y-3 overflow-auto pr-1">
              {transcript.data?.truncated && (
                <p className="text-xs text-dark-muted">
                  Showing the last {entries.length} of {transcript.data.total_entries} steps.
                </p>
              )}
              {entries.map((entry, index) => (
                <Entry key={index} entry={entry} />
              ))}
            </div>
          )}

          {showRawText && tail.isPending && (
            <p className="text-sm text-dark-muted">Reading this run's output…</p>
          )}
          {showRawText && tail.isError && (
            <p role="status" className="text-sm text-orange-200">
              This run's output could not be read just now. It is still on disk; the next poll
              will pick it up.
            </p>
          )}
          {showRawText && tail.data && empty && <p className="text-sm text-dark-muted">{empty}</p>}
          {showRawText && tail.data && !empty && (
            <pre
              data-run-output={run.run_id}
              className="max-h-80 overflow-auto whitespace-pre-wrap break-words text-xs leading-relaxed text-dark-text"
            >
              {text}
            </pre>
          )}
          {/* No "open the full output" link here on purpose: the run's own row carries
              one, two feet above this panel, and a second copy of it inside the panel is
              just a second thing to read. */}
          {showRawText && tail.data && !empty && (
            <p className="mt-2 text-xs text-dark-muted">
              {sourceNote(tail.data.source)} Last {tail.data.lines} lines.
            </p>
          )}
        </div>
      )}
    </div>
  );
}
