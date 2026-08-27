import { useState } from "react";

import type { FinishStepView, TaskFinishView } from "../api/types";

/**
 * What is happening to this task's branch, on the page that started it.
 *
 * Approving a task on a project with the scripted finish switched on starts a process
 * that rebases, runs the full gate, merges, rebuilds, restarts and verifies -- three or
 * four minutes of work on a click. Until task-321 the page said nothing at all about
 * any of it: the ball moved to `agent`/`work` carrying the approval's own prompt, the
 * Dispatch button stayed live, and a reader had no way to tell a finish in progress
 * from an approval nothing had picked up. Jeff, on task-296: *"when I click approve and
 * it auto finishes, there is no feedback really."*
 *
 * **The step table is the feedback, not the log.** A spawned finish writes its stdout
 * when the process ends, so for the whole of the time anybody is watching there is no
 * text to show. What exists instead is a record per step as it lands, and per gate
 * stage as it runs, which is both live and more legible than the terminal output would
 * have been. The log arrives at the end and is shown then, because that is when it
 * exists.
 *
 * **Renders for terminal finishes too.** A merge that landed two minutes ago is exactly
 * what a reader coming back to the page wants to see, and a panel that vanished the
 * moment the work completed would hide the answer at the moment it arrived.
 */

/** How often a watching browser re-reads the finish. Fast, because steps are short. */
export const FINISH_POLL_MS = 2_000;

/**
 * Poll while something is happening, never otherwise.
 *
 * Faster than the run tail's ten seconds, and the difference is justified rather than
 * inherited: a finish's state comes from small files this server already has open, with
 * no subprocess anywhere behind it, and its steps are seconds long -- a ten-second clock
 * would show a reader the rebase for the whole of the merge.
 */
export function finishPollInterval(finish: TaskFinishView | null): number | false {
  return finish?.live ? FINISH_POLL_MS : false;
}

/** Elapsed seconds as a human reads them. Server-computed; no client clock is involved. */
export function formatFinishElapsed(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "";
  const whole = Math.max(0, Math.round(seconds));
  if (whole < 60) return `${whole}s`;
  const minutes = Math.floor(whole / 60);
  return `${minutes}m ${String(whole % 60).padStart(2, "0")}s`;
}

/**
 * The headline: what happened, or is happening, in one sentence.
 *
 * Every branch names the consequence for the branch, because that is the only thing a
 * reader who just pressed Approve is actually asking about. "Escalated" on its own is
 * the enum talking.
 */
export function finishHeadline(finish: TaskFinishView): string {
  switch (finish.state) {
    case "starting":
      return "Finishing this task — starting";
    case "running":
      return "Finishing this task";
    case "finished":
      return finish.merge_commit
        ? `Merged as ${finish.merge_commit.slice(0, 8)} and verified live`
        : "Finished";
    case "escalated":
      return finish.merge_commit
        ? `Merged as ${finish.merge_commit.slice(0, 8)}, then stopped before it was done`
        : "Stopped — nothing was merged";
    case "declined":
      return "Nothing to merge";
    case "interrupted":
      return "The finish stopped without saying how it ended";
    default:
      return "Finish";
  }
}

/** The second line: what a reader should do about it, when there is anything to do. */
export function finishDetail(finish: TaskFinishView): string {
  switch (finish.state) {
    case "starting":
      return "The finish process is starting. Nothing has been rebased or merged yet.";
    case "running":
      return "Nothing is merged until the gate is green. This runs without an agent.";
    case "finished":
      return "The merge is done and the running server was checked for it.";
    case "escalated":
      return `It stopped at ${finish.stopped_at || "a step"}${
        finish.reason ? ` (${finish.reason})` : ""
      }. The task record says what it got done and what is left.`;
    case "declined":
      return `This task was never a finish candidate${
        finish.reason ? ` (${finish.reason})` : ""
      }, so the approval behaved as it always did.`;
    case "interrupted":
      return "Its process is gone and it wrote no ending — the machine restarted, or something killed it. The task record holds everything it managed to write.";
    default:
      return "";
  }
}

/** The gate line, or empty when the gate is not where this finish is. */
export function gateNote(finish: TaskFinishView): string {
  const gate = finish.gate;
  if (!gate) return "";
  // Every counter defaults to zero rather than being trusted: a gate too old to write
  // per-stage records sends none of them, and "of undefined stages" is worse than no
  // counter at all.
  const run = gate.stages_run ?? 0;
  const total = gate.stages_total ?? 0;
  if (gate.running) {
    const position = total > 0 ? ` — ${Math.max(run, 1)} of ${total}` : "";
    return gate.stage
      ? `Gate: ${gate.stage}${position}`
      : `Gate: running${total > 0 ? ` ${total} stages` : ""}`;
  }
  if (gate.passed) {
    return `Gate: green, ${run} of ${total} stages in ${formatFinishElapsed(gate.seconds)}`;
  }
  return `Gate: red at ${gate.failed_stage || "a stage"} after ${formatFinishElapsed(gate.seconds)} — a red gate never merges`;
}

/**
 * The badge, in the state's own terms.
 *
 * Not "Running" / "Done", which is what this was: a finish that stopped at a red gate
 * is not *done*, and the badge is the first thing read on the card. It is the one word
 * a reader glancing at the page takes away, so it has to be the true one.
 */
export function finishBadge(finish: TaskFinishView): string {
  if (finish.live) return finish.state === "starting" ? "Starting" : "Running";
  switch (finish.state) {
    case "finished":
      return "Done";
    case "escalated":
      return "Stopped";
    case "interrupted":
      return "Interrupted";
    case "declined":
      return "Not run";
    default:
      return "Ended";
  }
}

const STATE_CLASSES: Record<string, string> = {
  starting: "bg-sky-900 text-sky-200",
  running: "bg-sky-900 text-sky-200",
  finished: "bg-emerald-900 text-emerald-200",
  escalated: "bg-orange-900 text-orange-100",
  declined: "bg-slate-700 text-slate-200",
  interrupted: "bg-orange-900 text-orange-100",
};

const STEP_MARK: Record<string, string> = {
  done: "✓",
  skipped: "–",
  stopped: "✕",
  running: "▸",
};

const STEP_CLASSES: Record<string, string> = {
  done: "text-emerald-300",
  skipped: "text-dark-muted",
  stopped: "text-orange-300",
  running: "text-sky-300",
};

function Step({ step, gate }: { step: FinishStepView; gate: string }) {
  // The gate's own progress rides on the gate step rather than in a line of its own,
  // because it is an answer to "how far into that step are you" and a reader scanning
  // the table should not have to correlate two places to get it.
  const extra = step.name === "gate" && gate ? gate : "";
  return (
    <li className="flex gap-3 py-1" data-finish-step={step.name} data-step-state={step.state}>
      <span className={`w-4 shrink-0 text-center ${STEP_CLASSES[step.state] ?? ""}`} aria-hidden="true">
        {STEP_MARK[step.state] ?? "·"}
      </span>
      <span className="w-20 shrink-0 font-mono text-xs leading-6">{step.name}</span>
      <span className="min-w-0 flex-1 text-sm">
        <span className={step.state === "running" ? "text-sky-200" : "text-dark-text"}>
          {step.state === "running" ? step.meaning || "Running" : step.detail || step.meaning}
        </span>
        {extra && <span className="ml-2 text-dark-muted">{extra}</span>}
      </span>
      {(step.seconds ?? 0) > 0 && (
        <span className="shrink-0 font-mono text-xs text-dark-muted">
          {(step.seconds ?? 0).toFixed(1)}s
        </span>
      )}
    </li>
  );
}

export type FinishPanelProps = {
  /** Null when no finish has ever run for this task, which is almost every task. */
  finish: TaskFinishView | null;
};

export function FinishPanel({ finish }: FinishPanelProps) {
  // Open while something is happening, and it stays open when that finish ends: a panel
  // that collapses itself at the moment the work completes hides the answer at the one
  // moment the person watching wanted it. A finish that was already over when the page
  // loaded starts collapsed.
  const [open, setOpen] = useState(Boolean(finish?.live));
  if (!finish) return null;

  const gate = gateNote(finish);
  const badge = STATE_CLASSES[finish.state] ?? "bg-slate-700 text-slate-200";
  const elapsed = formatFinishElapsed(finish.elapsed_seconds);
  const steps = finish.steps ?? [];

  return (
    <section
      className="space-y-3 rounded-xl border-2 border-indigo-700/50 bg-indigo-950/30 p-4 min-[820px]:p-6"
      aria-label="Finish"
      data-finish-state={finish.state}
      data-finish-live={finish.live ? "yes" : "no"}
    >
      <div className="flex flex-wrap items-center gap-3">
        <span className={`rounded px-2 py-0.5 text-xs font-semibold ${badge}`}>
          {finishBadge(finish)}
        </span>
        <h2 className="text-lg font-semibold text-indigo-200">{finishHeadline(finish)}</h2>
        {elapsed && (
          <span className="text-sm text-dark-muted">
            {finish.live ? "for " : "took "}
            {elapsed}
          </span>
        )}
      </div>

      <p className="text-sm text-dark-muted">{finishDetail(finish)}</p>

      {finish.branch && (
        <p className="text-sm text-dark-muted">
          Branch <strong className="font-mono text-dark-text">{finish.branch}</strong>
          {finish.live && gate && !steps.some((step) => step.name === "gate") && (
            <span className="ml-2">{gate}</span>
          )}
        </p>
      )}

      {steps.length > 0 && (
        <ol className="divide-y divide-dark-border rounded-lg border border-dark-border bg-dark-surface px-3 py-1">
          {steps.map((step) => (
            <Step key={`${step.name}-${step.state}`} step={step} gate={gate} />
          ))}
        </ol>
      )}

      {steps.length === 0 && finish.live && (
        <p className="text-sm text-dark-muted" role="status">
          No step has been recorded yet. The first one lands within a couple of seconds.
        </p>
      )}

      <div className="flex flex-wrap items-center gap-3">
        <a
          href={finish.output_url}
          target="_blank"
          rel="noreferrer"
          className="touch-target rounded-lg border border-dark-border px-3 text-sm text-blue-300 hover:bg-dark-border"
        >
          View output
        </a>
        {finish.output_tail && (
          <button
            type="button"
            aria-expanded={open}
            aria-controls="finish-output"
            onClick={() => setOpen((current) => !current)}
            className="flex items-center gap-2 text-sm font-semibold text-blue-300 hover:text-blue-200"
          >
            <span aria-hidden="true">{open ? "▾" : "▸"}</span>
            Output
          </button>
        )}
      </div>

      {finish.output_tail && open && (
        <div id="finish-output" className="rounded-lg border border-dark-border bg-dark-bg p-3">
          <pre
            data-finish-output={finish.task_id}
            className="max-h-80 overflow-auto whitespace-pre-wrap break-words text-xs leading-relaxed text-dark-text"
          >
            {finish.output_tail}
          </pre>
          <p className="mt-2 text-xs text-dark-muted">
            {finish.output_source === "gate-log"
              ? "The gate's own output."
              : "What the finish process printed."}{" "}
            The end of it; “View output” has all of it.
          </p>
        </div>
      )}

      {!finish.output_tail && finish.live && (
        <p className="text-xs text-dark-muted">
          A finish writes its output when the process ends — no agent is involved, so
          there is no session transcript. The steps above are what is live.
        </p>
      )}
    </section>
  );
}
