import type { AcceptanceCriterion, CheckOutcome, LogEntry } from "../api/generated";
import { RefusalNote, type DispatchRefusal } from "./DispatchPanel";

/**
 * Check results on the acceptance list, and the control that produces them (task-152).
 *
 * task-147 gave a criterion an executable `check` and gave the API a `POST .../check`
 * that runs them. Everything it wrote was legible from the CLI and from nowhere else:
 * the page showed a criterion's `status` moving to `met` or `failed` with no indication
 * that a command had decided it, how long it took, or what it printed on the way down.
 *
 * **The results are read back out of the log, not out of a second store.** One pass
 * writes exactly one `check_result` entry naming every criterion it decided, so the log
 * already holds the whole history and a cache beside it could only ever disagree with
 * it. {@link latestCheckOutcomes} is that read.
 */

/** A criterion's standing with respect to its check, as the page shows it. */
export type CheckStanding =
  /** Exit 0 on the newest pass that decided it. */
  | { kind: "passed"; outcome: CheckOutcome }
  /** Anything but exit 0 -- including a check that could not be started at all. */
  | { kind: "failed"; outcome: CheckOutcome }
  /** It has a check and no pass has decided it yet. Distinct from having failed. */
  | { kind: "never-run" };

function asRecord(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null ? (value as Record<string, unknown>) : {};
}

/**
 * Read one outcome out of a `check_result` payload, or null if it is not one.
 *
 * The server validates this on the way in -- `CheckOutcome` refuses a failure that
 * names neither an exit code nor a cause -- so this is not re-validating. It is
 * narrowing: `LogEntry.data` is `Record<string, unknown>` on this side of the wire,
 * and an entry written by a future build may carry fields this one does not know.
 */
function readOutcome(raw: unknown): CheckOutcome | null {
  const value = asRecord(raw);
  if (typeof value.id !== "string" || !value.id) return null;
  if (value.status !== "met" && value.status !== "failed") return null;
  return {
    id: value.id,
    status: value.status,
    exit_code: typeof value.exit_code === "number" ? value.exit_code : null,
    duration_seconds: typeof value.duration_seconds === "number" ? value.duration_seconds : 0,
    cause: typeof value.cause === "string" ? value.cause : null,
    output_tail: typeof value.output_tail === "string" ? value.output_tail : null,
  };
}

/**
 * Each criterion's standing, from the newest `check_result` entry that named it.
 *
 * **Newest pass that named it, not newest pass** -- and the difference is the whole
 * reason this is a scan rather than a lookup in `log[log.length - 1]`. A pass runs the
 * checks the task carried at the moment it ran, so a criterion added since is in no
 * entry at all, and a criterion whose `check` was removed is named in the newer pass's
 * `unchecked` list. Reading only the newest entry would show the first as never-run
 * (right, by luck) and the second as still passing (wrong, on a check that no longer
 * exists).
 *
 * Being named in `unchecked` is therefore an answer and not a gap: it says this pass
 * looked at the criterion and found nothing to run, which retires every older result
 * for it. That is why the map holds a `never-run` standing rather than omitting the key.
 */
export function latestCheckOutcomes(log: ReadonlyArray<LogEntry>): Map<string, CheckStanding> {
  const standings = new Map<string, CheckStanding>();
  for (const entry of [...log].reverse()) {
    if (entry.type !== "check_result") continue;
    const data = asRecord(entry.data);
    for (const raw of Array.isArray(data.results) ? data.results : []) {
      const outcome = readOutcome(raw);
      if (!outcome || standings.has(outcome.id)) continue;
      standings.set(outcome.id, {
        kind: outcome.status === "met" ? "passed" : "failed",
        outcome,
      });
    }
    for (const id of Array.isArray(data.unchecked) ? data.unchecked : []) {
      if (typeof id === "string" && id && !standings.has(id)) {
        standings.set(id, { kind: "never-run" });
      }
    }
  }
  return standings;
}

/** True when at least one criterion carries a check, which is what the button needs. */
export function hasExecutableChecks(
  acceptance: ReadonlyArray<AcceptanceCriterion> | null | undefined,
): boolean {
  return (acceptance ?? []).some((criterion) => (criterion.check?.length ?? 0) > 0);
}

/**
 * How long a check took, in the units a reader of this page thinks in.
 *
 * Sub-second is the common case for a fast unit test and `0.0s` would read as "it did
 * not run", which is precisely the state this line has to stay distinguishable from.
 */
export function formatDuration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return "an unknown time";
  if (seconds < 1) return `${Math.max(1, Math.round(seconds * 1000))}ms`;
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes}m ${Math.round(seconds - minutes * 60)}s`;
}

/** Why a failure failed, in a clause a person reads rather than a cause enum. */
const CAUSES: Record<string, string> = {
  timeout: "it ran out of time",
  not_started: "it could not be started",
  pass_timeout: "the pass ran out of time before it finished",
};

/**
 * The failure's reason, as a trailing clause or the empty string.
 *
 * The exit code is preferred over the cause when there is one, because a process that
 * exited says more about what to do next than the category the server filed it under.
 */
export function failureReason(outcome: CheckOutcome): string {
  if (typeof outcome.exit_code === "number") return `exited ${outcome.exit_code}`;
  const cause = outcome.cause ?? "";
  return CAUSES[cause] ?? (cause ? `it failed: ${cause}` : "");
}

/** The one sentence a standing is worth, with never-run kept apart from failed. */
export function checkSentence(standing: CheckStanding): string {
  if (standing.kind === "never-run") return "Check not yet run";
  const took = formatDuration(standing.outcome.duration_seconds);
  if (standing.kind === "passed") return `Check passed in ${took}`;
  const reason = failureReason(standing.outcome);
  return reason ? `Check failed in ${took} — ${reason}` : `Check failed in ${took}`;
}

const STANDING_CLASSES: Record<CheckStanding["kind"], string> = {
  passed: "text-emerald-300",
  failed: "text-red-300",
  "never-run": "text-dark-muted",
};

/**
 * One criterion's check result: the sentence, and the output behind a disclosure.
 *
 * `<details>` rather than a toggle of our own, because the output tail is the long tail
 * of this page in both senses -- most criteria never show one, and the one that does can
 * be hundreds of lines. Collapsed by default and opened by the reader is the browser's
 * own answer to that, and it survives with no JavaScript at all.
 */
export function CheckResult({ standing }: { standing: CheckStanding }) {
  const tail = standing.kind === "failed" ? standing.outcome.output_tail : null;
  return (
    <div className="mt-2 text-xs" data-check-standing={standing.kind}>
      <span className={STANDING_CLASSES[standing.kind]}>{checkSentence(standing)}</span>
      {tail && (
        <details className="group mt-1">
          {/* The marker is drawn rather than left to the browser's own. This app's
              reset takes `list-style` off every list, which takes the triangle off a
              `<summary>` with it -- and reviewed in a browser, "Output" with no
              triangle reads as a label rather than as something to press. */}
          <summary className="touch-target flex cursor-pointer list-none items-center gap-1 text-dark-muted hover:text-dark-text">
            <span aria-hidden="true" className="transition-transform group-open:rotate-90">
              ▸
            </span>
            <span>Output</span>
          </summary>
          <pre className="mt-1 max-h-64 overflow-auto whitespace-pre-wrap rounded border border-dark-border bg-dark-bg p-2 text-dark-muted">
            {tail}
          </pre>
        </details>
      )}
    </div>
  );
}

export type AcceptanceProps = {
  acceptance: ReadonlyArray<AcceptanceCriterion>;
  log: ReadonlyArray<LogEntry>;
  /**
   * Runs this task's checks. Absent on a page that cannot offer the control at all.
   *
   * It takes no argument and never will. The endpoint behind it takes no body either:
   * what runs is whatever the record already says, so there is no request shape in
   * which a browser could name a command for this machine to execute. See the note on
   * {@link AcceptanceSection}.
   */
  onRunChecks?: () => void | Promise<void>;
  running?: boolean;
  /**
   * The gate's own answer when it refused, or null.
   *
   * Carried as the refusal rather than as a string because it is a *dispatch* refusal:
   * the check route passes `assert_dispatch_permitted` and answers under the same
   * reason codes the Dispatch panel already renders, so it is rendered by the same
   * component and never paraphrased on the way.
   */
  refusal?: DispatchRefusal | null;
};

/**
 * The acceptance list with each criterion's check result, and a Run checks button.
 *
 * **The button is the only new thing the browser can do, and it carries no payload.**
 * A `check` is an argv this machine executes, so a surface that could write one is a
 * surface that could run anything here under somebody else's hand — the same argument
 * that keeps a runner's argv out of the browser. Running what the record already says
 * is a different act from choosing what it says, and only the first is offered: this
 * component renders `check` nowhere, and the mutation behind `onRunChecks` sends a path
 * and no body. Editing acceptance from the browser is task-382, which inherits this.
 */
export function AcceptanceSection({
  acceptance,
  log,
  onRunChecks,
  running = false,
  refusal = null,
}: AcceptanceProps) {
  const standings = latestCheckOutcomes(log);
  const runnable = hasExecutableChecks(acceptance);
  return (
    <div>
      <div className="mb-2 flex flex-wrap items-center gap-3">
        <h2 className="text-lg font-semibold">Acceptance</h2>
        {runnable && onRunChecks && (
          <button
            type="button"
            className="touch-target rounded-lg border border-sky-500/60 bg-sky-900/40 px-3 text-sm font-semibold text-sky-100 hover:bg-sky-900/60 disabled:opacity-60"
            disabled={running}
            onClick={() => void onRunChecks()}
          >
            {running ? "Running checks…" : "Run checks"}
          </button>
        )}
      </div>
      {/* `answered` because this only ever appears after somebody pressed the button:
          it describes their action, not the state of the world, which is the
          distinction that decides whether a screen reader interrupts. */}
      {refusal && (
        <div className="mb-2">
          <RefusalNote refusal={refusal} />
        </div>
      )}
      <ul className="space-y-2">
        {acceptance.map((criterion) => {
          // Only a criterion that carries a check gets a check line. One with none is
          // prose a person decides, and "not yet run" under it would invent a machine
          // verdict this criterion was never going to have.
          const standing = (criterion.check?.length ?? 0)
            ? standings.get(criterion.id) ?? { kind: "never-run" as const }
            : null;
          return (
            <li
              className="rounded-lg border border-dark-border bg-dark-bg p-3 text-sm"
              key={criterion.id}
            >
              <span className="mr-2 uppercase text-dark-muted">{criterion.status ?? "pending"}</span>
              {criterion.text}
              {criterion.verify && (
                <code className="mt-1 block text-xs text-dark-muted">{criterion.verify}</code>
              )}
              {standing && <CheckResult standing={standing} />}
            </li>
          );
        })}
      </ul>
    </div>
  );
}
