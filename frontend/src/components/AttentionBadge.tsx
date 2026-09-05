import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { getAttentionApiProjectsProjectIdAttentionGetOptions } from "../api/generated/@tanstack/react-query.gen";

/**
 * How many tasks are stopped waiting on the person reading the page (task-338).
 *
 * **Reported defect:** on the Tasks tab -- or any surface that is not the Dashboard --
 * nothing told you work had stopped on you. The Dashboard has said so all along, in
 * the red "N Tasks Blocked on You" panel, which is precisely the wrong place: you only
 * see it once you have already gone to look.
 *
 * The legacy Jinja header carried this badge on its Tasks link and the React port
 * dropped it, so this is a restoration rather than a new idea, and it restores the
 * *number* as well as the dot: `tests/test_attention_tiers.py` records why it counts
 * `blocks_human` only. A draft parked on an unmade decision is backlog, not a
 * blockage; counting it made a badge that never reached zero, which could only be read
 * by opening it -- the work the badge exists to save.
 */

/**
 * Above this the badge reads `9+`, as the legacy header did.
 *
 * Not a truncation for space: a two-digit red circle is still legible. It is that the
 * difference between nine and fourteen changes nothing about what you do next, while
 * the width of the pill jitters every time a task closes.
 */
export const ATTENTION_BADGE_MAX = 9;

/**
 * The count, from an endpoint that returns one integer.
 *
 * Not the dashboard query, though it holds the same number. That payload is every
 * task record in the project -- 900KB against this repository's own corpus -- and the
 * header renders on every surface, so the badge would have made the Tasks tab pay a
 * dashboard's worth of bytes for a number.
 *
 * Freshness is `LiveUpdates`, not a clock of its own: this number moves when a task
 * file is written and at no other time, which is exactly what the project revision
 * tracks. The query id is in `PROJECT_TASK_QUERY_IDS` for that, and the drift test
 * there is what stops a future endpoint being added and quietly never refetching.
 */
export function useHumanAttention(projectId: string): number | null {
  const query = useQuery(
    getAttentionApiProjectsProjectIdAttentionGetOptions({ path: { project_id: projectId } }),
  );
  return query.data?.blocking ?? null;
}

/**
 * The badge itself, or nothing at all when nothing is waiting.
 *
 * Rendering a zero -- which is what the Runs badge beside it does -- would be the
 * wrong call here. That one is a readout, and zero running is information. This one is
 * an alarm, and an alarm that is always on the screen stops being read; it also spends
 * width in a bar that `NAV_INLINE_MIN_PX` says has none to spare.
 */
export function AttentionBadge({ count, projectId }: { count: number | null; projectId: string }) {
  if (!count || count <= 0) return null;
  const label = `${count} ${count === 1 ? "task is" : "tasks are"} waiting on you`;
  return (
    <Link
      to={`/p/${encodeURIComponent(projectId)}`}
      data-testid="attention-badge"
      aria-label={label}
      title={label}
      className="flex h-6 min-w-6 shrink-0 items-center justify-center rounded-full bg-red-500 px-1.5 text-xs font-bold text-white hover:bg-red-400"
    >
      {count > ATTENTION_BADGE_MAX ? `${ATTENTION_BADGE_MAX}+` : count}
    </Link>
  );
}
