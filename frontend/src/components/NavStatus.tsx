import type { StatusCategory } from "../api/types";

import { ATTENTION_BADGE_MAX } from "./AttentionBadge";
import { categoryStyle } from "./StatusChip";

/**
 * The header's status counts, carried by the Dashboard tab (task-588, task-608).
 *
 * Until task-588 the bar carried two badges: a green running count on the Runs link
 * (task-328) and a red waiting-on-you count beside the project switcher (task-338). The
 * owner never used the Runs tab, so the green badge sat on a destination nobody went to,
 * and the two numbers that answer "what is this machine doing for me" were in two places.
 * task-588 made them two dots on the Dashboard tab; task-608 made it three, in the
 * status vocabulary's own colours so the tab says a state the way every chip says it:
 *
 * - **waiting**, `needs_you` red -- tasks stopped waiting on you, `attention.blocking`
 *   exactly as the old badge counted it. This project's.
 * - **working**, `working` blue -- runs being worked right now. Machine-wide.
 * - **landing**, `finishing` violet -- merges landing, counted the way the slot board
 *   draws Landing. Machine-wide, and never also counted as working: `runCounts`.
 *
 * **The number is inside the dot**, which grows into a small round pill, so three
 * counts cost no more width than two dots with digits beside them did. Each is capped at
 * {@link ATTENTION_BADGE_MAX}+ so the tab stops changing width.
 *
 * **Not a link of its own.** The first cut on task-588 drew the counts as a pill link
 * beside the Dashboard link, with a slot fraction (`2/3`) in front. The owner rejected
 * both on review: two links side by side going to one page, and a number nobody reads.
 * So this renders plain content, and `PrimaryNav` puts it *inside* the one Dashboard link.
 *
 * **A zero is drawn, and recedes.** A part that comes and goes cannot be told from one
 * that has not loaded. So a zero is a slate pill with its 0 still legible, and a
 * non-zero is the colour.
 */
export function NavCounts({
  waiting,
  working,
  landing,
}: {
  waiting: number | null;
  working: number;
  landing: number;
}) {
  return (
    <span className="inline-flex items-center gap-1.5 text-[11px] font-semibold tabular-nums">
      <Count testId="nav-status-waiting" count={Math.max(0, waiting ?? 0)} category="needs_you" />
      <Count testId="nav-status-working" count={Math.max(0, working)} category="working" />
      <Count testId="nav-status-landing" count={Math.max(0, landing)} category="finishing" />
    </span>
  );
}

/** The counts in words, for the Dashboard link's accessible name and tooltip. */
export function navStatusLabel(waiting: number | null, working: number, landing: number): string {
  return (
    `${Math.max(0, waiting ?? 0)} waiting on you · ` +
    `${Math.max(0, working)} being worked · ${Math.max(0, landing)} landing`
  );
}

function Count({
  testId,
  count,
  category,
}: {
  testId: string;
  count: number;
  category: StatusCategory;
}) {
  const lit = count > 0;
  return (
    <span
      data-testid={testId}
      data-count={count}
      data-status-category={lit ? category : undefined}
      className={`inline-flex h-5 min-w-5 items-center justify-center rounded-full border px-1 leading-none ${
        lit ? "" : "border-slate-600 bg-slate-700 text-slate-300"
      }`}
      style={lit ? categoryStyle(category) : undefined}
    >
      {count > ATTENTION_BADGE_MAX ? `${ATTENTION_BADGE_MAX}+` : count}
    </span>
  );
}
