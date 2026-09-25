import { ATTENTION_BADGE_MAX } from "./AttentionBadge";

/**
 * The header's status counts, carried by the Dashboard tab (task-588).
 *
 * Until task-588 the bar carried two badges: a green running count on the Runs link
 * (task-328) and a red waiting-on-you count beside the project switcher (task-338). The
 * owner never used the Runs tab, so the green badge sat on a destination nobody went to,
 * and the two numbers that answer "what is this machine doing for me" were in two places.
 * They are two dots on the Dashboard tab now:
 *
 * - **red** -- tasks stopped waiting on you, `attention.blocking` exactly as the old
 *   badge counted it, capped at {@link ATTENTION_BADGE_MAX}+ the way it was.
 * - **green** -- being worked right now: `runningCount`, runs plus finishes.
 *
 * **Not a link of its own.** The first cut on this task drew the counts as a pill link
 * beside the Dashboard link, with a slot fraction (`2/3`) in front. The owner rejected
 * both on review: two links side by side going to one page, and a number nobody reads.
 * So this renders plain content, and `PrimaryNav` puts it *inside* the one Dashboard link.
 *
 * **A zero is drawn, and recedes.** The old red badge disappeared at zero; beside a
 * second count, a part that comes and goes cannot be told from one that has not loaded.
 * So a zero is a grey dot and muted digits, and a non-zero is the colour.
 */
export function NavCounts({ waiting, working }: { waiting: number | null; working: number }) {
  return (
    <span className="inline-flex items-center gap-2.5 text-sm font-semibold tabular-nums">
      <Count testId="nav-status-waiting" count={Math.max(0, waiting ?? 0)} dot="bg-red-500" />
      <Count testId="nav-status-working" count={Math.max(0, working)} dot="bg-green-500" />
    </span>
  );
}

/** The counts in words, for the Dashboard link's accessible name and tooltip. */
export function navStatusLabel(waiting: number | null, working: number): string {
  return `${Math.max(0, waiting ?? 0)} waiting on you · ${Math.max(0, working)} being worked`;
}

function Count({ testId, count, dot }: { testId: string; count: number; dot: string }) {
  const lit = count > 0;
  return (
    <span
      data-testid={testId}
      data-count={count}
      className={`inline-flex items-center gap-1 ${lit ? "text-white" : "text-dark-muted"}`}
    >
      <span
        aria-hidden="true"
        className={`h-2.5 w-2.5 rounded-full ${lit ? dot : "bg-slate-600"}`}
      />
      {count > ATTENTION_BADGE_MAX ? `${ATTENTION_BADGE_MAX}+` : count}
    </span>
  );
}
