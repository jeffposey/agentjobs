import type { Ball, BallReason, Lifecycle, Outcome } from "./generated";

// Each expected error turns an accidentally widened generated enum into a
// TypeScript build failure.
export function lifecycleRejectsInvalidComparison(value: Lifecycle) {
  // @ts-expect-error Lifecycle is a closed generated union.
  return value === "in_progress";
}

export function ballRejectsInvalidComparison(value: Ball) {
  // @ts-expect-error Ball is a closed generated union.
  return value === "nobody";
}

export function ballReasonRejectsInvalidComparison(value: BallReason) {
  // @ts-expect-error BallReason is a closed generated union.
  return value === "unknown";
}

export function outcomeRejectsInvalidComparison(value: Outcome) {
  // @ts-expect-error Outcome is a closed generated union.
  return value === "failed";
}
