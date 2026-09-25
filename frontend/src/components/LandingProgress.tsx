import { useEffect, useState } from "react";

import type { LandingEstimate } from "../api/types";

/**
 * How far along a landing is, and roughly when it will be done (task-586).
 *
 * **Nothing here estimates anything.** The server computes `progress`, `eta_seconds`
 * and `overrun` once, from the finish's own records and the project's history, and the
 * task list, the task page and the slot board all receive that same object -- so no
 * two surfaces can draw a different bar for one finish. This file only says it in
 * words and draws it.
 *
 * **Between polls the bar counts down from the server's answer**, which is the one
 * thing done on the client: `eta_seconds` is "from when this response was built", so
 * subtracting the seconds since it arrived is arithmetic on the server's number, not a
 * second model. It stops at the floor rather than reaching zero, and the next poll
 * replaces it.
 *
 * Three rules the words keep, each a way an estimate lies:
 *
 * - **Never done before it is.** The server caps progress below 100% while the finish
 *   runs, and under a minute reads "under a minute left", never "0 min left".
 * - **Never a negative or a frozen number.** An overrun reads "taking longer than
 *   usual", and the bar stays where it got to rather than going back.
 * - **Never an invented wait.** On the merge runway there is no number at all: the bar
 *   is indeterminate and says what it is waiting for.
 */

/** The seconds of countdown the client may apply before it stops trusting its clock. */
const COUNTDOWN_LIMIT_S = 30;

/** The most a client-side countdown may move the bar, which the server also caps at. */
const PROGRESS_CAP = 0.95;

/** Whole minutes as a person says them: "about 4 min". */
function aboutMinutes(seconds: number): string {
  return `${Math.max(1, Math.round(seconds / 60))} min`;
}

/**
 * The estimate in a few words, for a row: "~3 min left", "taking longer than usual",
 * "waiting for the merge runway". Empty when there is nothing to estimate from, so the
 * caller shows elapsed time alone.
 */
export function landingWords(estimate: LandingEstimate | null | undefined, etaSeconds?: number | null): string {
  if (!estimate) return "";
  if (estimate.kind === "runway") return "waiting for the merge runway";
  if (estimate.kind !== "estimate") return "";
  if (estimate.overrun) return "taking longer than usual";
  const eta = etaSeconds ?? estimate.eta_seconds;
  if (eta === null || eta === undefined) return "";
  if (eta < 60) return "under a minute left";
  return `~${aboutMinutes(eta)} left`;
}

/**
 * The same in a sentence, for the task page: "about 4 min left (typical: 6 min)".
 */
export function landingSentence(estimate: LandingEstimate | null | undefined, etaSeconds?: number | null): string {
  if (!estimate) return "";
  const typical =
    estimate.typical_seconds !== null && estimate.typical_seconds !== undefined
      ? ` (typical: ${aboutMinutes(estimate.typical_seconds)})`
      : "";
  if (estimate.kind === "runway") {
    return "Waiting for the merge runway — another landing holds it, so no time is estimated yet.";
  }
  if (estimate.kind === "no_history") {
    return "Not enough finished landings in this project to estimate from yet.";
  }
  if (estimate.overrun) return `Taking longer than usual${typical}.`;
  const eta = etaSeconds ?? estimate.eta_seconds;
  if (eta === null || eta === undefined) return "";
  const left = eta < 60 ? "under a minute left" : `about ${aboutMinutes(eta)} left`;
  return `Estimate: ${left}${typical}.`;
}

/**
 * The estimate as of now: the server's answer, counted down since it arrived.
 *
 * Progress moves at the rate that would reach the end at the ETA, and never past the
 * cap; an overrun does not move at all. After `COUNTDOWN_LIMIT_S` without a fresh answer
 * it holds still, because a bar that keeps moving on a stale answer is claiming to know
 * something it does not.
 */
export function counted(
  estimate: LandingEstimate | null | undefined,
  secondsSince: number,
): { progress: number | null; eta: number | null } {
  if (!estimate || estimate.kind !== "estimate") return { progress: null, eta: null };
  const progress = estimate.progress ?? null;
  const eta = estimate.eta_seconds ?? null;
  if (progress === null || eta === null || estimate.overrun) return { progress, eta };
  const since = Math.min(Math.max(0, secondsSince), COUNTDOWN_LIMIT_S);
  const left = Math.max(eta - since, 1);
  const rate = eta > 0 ? (1 - progress) / eta : 0;
  return { progress: Math.min(PROGRESS_CAP, progress + rate * since), eta: left };
}

/** Re-render once a second while an estimate is counting down. */
function useSecondsSince(estimate: LandingEstimate | null | undefined): number {
  const [since, setSince] = useState(0);
  useEffect(() => {
    setSince(0);
    if (!estimate || estimate.kind !== "estimate" || estimate.overrun) return undefined;
    const arrived = Date.now();
    const timer = window.setInterval(() => setSince((Date.now() - arrived) / 1000), 1000);
    return () => window.clearInterval(timer);
  }, [estimate]);
  return since;
}

export function LandingBar({
  estimate,
  size = "thin",
  label,
}: {
  estimate: LandingEstimate | null | undefined;
  size?: "thin" | "wide";
  /** What the bar is the progress of, for a screen reader. */
  label?: string;
}) {
  const since = useSecondsSince(estimate);
  if (!estimate || estimate.kind === "no_history") return null;
  const { progress } = counted(estimate, since);
  const height = size === "wide" ? "h-2" : "h-1";
  const runway = estimate.kind === "runway";
  const percent = progress === null ? null : Math.round(progress * 100);
  return (
    <div
      role="progressbar"
      aria-label={label ?? "Landing progress, an estimate"}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={percent ?? undefined}
      aria-valuetext={runway ? "waiting for the merge runway" : landingWords(estimate)}
      title={estimate.basis}
      data-landing-kind={estimate.kind}
      data-landing-overrun={estimate.overrun ? "yes" : "no"}
      data-landing-progress={percent ?? undefined}
      className={`relative w-full overflow-hidden rounded-full bg-dark-border ${height}`}
    >
      {runway ? (
        <div className="absolute inset-y-0 left-0 w-1/3 animate-pulse rounded-full bg-violet-400/60" />
      ) : (
        <div
          className={`h-full rounded-full transition-[width] duration-1000 ease-linear ${
            estimate.overrun ? "bg-amber-400" : "bg-violet-400"
          }`}
          style={{ width: `${percent ?? 0}%` }}
        />
      )}
    </div>
  );
}

/** "4m 10s": the server's elapsed seconds, as the finish panel spells them. */
export function elapsedWords(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "";
  const whole = Math.max(0, Math.round(seconds));
  if (whole < 60) return `${whole}s`;
  return `${Math.floor(whole / 60)}m ${String(whole % 60).padStart(2, "0")}s`;
}

/**
 * A row's landing line: a thin bar and a few words. Elapsed only when there is nothing
 * to estimate from -- the server's elapsed seconds, never this browser's clock.
 */
export function LandingProgress({
  estimate,
  className = "",
}: {
  estimate: LandingEstimate | null | undefined;
  className?: string;
}) {
  const since = useSecondsSince(estimate);
  if (!estimate) return null;
  const { eta } = counted(estimate, since);
  const elapsed = elapsedWords(estimate.elapsed_seconds);
  const words = landingWords(estimate, eta) || (elapsed ? `${elapsed} so far, no estimate yet` : "");
  if (!words && estimate.kind === "no_history") return null;
  return (
    <span
      className={`flex min-w-0 items-center gap-2 ${className}`}
      data-landing-row=""
      title={estimate.basis}
    >
      {estimate.kind !== "no_history" && (
        <span className="w-16 shrink-0">
          <LandingBar estimate={estimate} />
        </span>
      )}
      <span className="min-w-0 truncate text-xs text-dark-muted" data-landing-words="">
        {words}
      </span>
    </span>
  );
}
