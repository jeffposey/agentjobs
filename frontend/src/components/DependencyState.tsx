import type { StatusCategory, TaskSummaryRead } from "../api/types";
import { type ChipMotion, StatusChip, taskMotion } from "./StatusChip";

/**
 * A task's chip and the reason line under it.
 *
 * **The word and the colour are the server's** (task-562): `display_status` and
 * `status_category` come from one function, `models_v2.task_status`, which already
 * folds in every fact this component used to rewrite the word from -- unmet needs,
 * open sub-tasks, a `needs` cycle, a queued dispatch, a live finish. Before that this
 * function overwrote the server's word in five branches, so the list, the live runs
 * board and the task page each spoke their own dialect. Nothing here decides a word now.
 *
 * What stays here is the reason line: the detail that had to leave the one-word chip --
 * the blocker, the reset time, the step a finish is on, the prompt a person is being
 * asked to answer.
 */
export function dependencyState(task: TaskSummaryRead): {
  category: StatusCategory;
  label: string;
  reasons: string[];
  motion: ChipMotion | null;
} {
  return {
    category: task.status_category,
    label: task.display_status,
    reasons: reasonsFor(task),
    motion: taskMotion(task),
  };
}

/**
 * The reason line, in the same order the server ranks the facts in, so the sentence
 * under a chip is always about the fact the chip names.
 */
function reasonsFor(task: TaskSummaryRead): string[] {
  if (task.lifecycle === "closed") return [];
  if (task.live_finish) {
    const step = task.live_finish.step_meaning || task.live_finish.current_step;
    return [step ? `Merging this branch. ${step}.` : "Merging this branch."];
  }
  const cycles = task.needs_cycles ?? [];
  if (cycles.length > 0) return cycles.map((cycle) => `Cycle: ${cycle.join(" → ")}`);
  if (task.ball === "human" || (task.ball === "agent" && task.ball_reason === "hold")) {
    return task.ball_prompt ? [task.ball_prompt] : [];
  }
  const unmet = task.unmet_needs ?? [];
  if (unmet.length > 0) return unmet.map((reason) => `Waiting for ${reason}`);
  if (task.ball === "external") {
    if (task.self_clearing_wait) {
      // The reset time moved here from the chip (task-562), and in the reader's own
      // zone: a label derived on the server could only say UTC.
      const resets = task.self_clearing_wait.resets_at;
      return [
        resets
          ? `Resumes by itself when the quota resets at ${formatResetTime(resets)}.`
          : "Resumes by itself when the quota resets.",
      ];
    }
    return [task.ball_prompt || "Waiting for an external dependency."];
  }
  if (task.status_category === "working") return [];
  const children = task.open_children_count ?? 0;
  if (children > 0) {
    // Not "must finish first": since task-164 an epic can be claimed, and what that
    // hands you is the supervisor's seat rather than the children's work.
    return [
      `${children} open sub-task${children === 1 ? "" : "s"} to finish. ` +
        "Claim it to supervise them — a session per child, not the work itself.",
    ];
  }
  if (task.queued_dispatch) {
    return [
      task.queued_dispatch.paused_by
        ? `A dispatch is waiting for a slot, and nothing is being tried on its credential while ${task.queued_dispatch.paused_by} is open.`
        : "A dispatch of this task is waiting for a free slot. Nothing has started.",
    ];
  }
  return [];
}

function formatResetTime(iso: string): string {
  const when = new Date(iso);
  if (Number.isNaN(when.getTime())) return iso;
  return when.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

/**
 * The archived flag, drawn beside the chip rather than inside it (task-562).
 *
 * Neutral on purpose: it is not a status, so it takes no status colour. It used to be
 * an "(archived)" suffix on the chip's word, which made one outcome two labels.
 */
export function ArchivedTag() {
  return (
    <span
      data-testid="archived-tag"
      className="inline-flex rounded border border-dark-border px-1.5 text-xs text-dark-muted"
    >
      Archived
    </span>
  );
}

export function DependencyState({ task, compact = false }: { task: TaskSummaryRead; compact?: boolean }) {
  const state = dependencyState(task);
  // Most of these reasons are a `ball_prompt`, which is written for someone who has the
  // task open -- routinely several paragraphs. In a list column that is not a summary,
  // it is the column: one task parked on review wrapped to 2070px inside a 194px cell
  // and made its row 2091px tall, against a median of 77px, so the first screen of the
  // task list rendered as an empty void with that one row's prose off to the right.
  // Compact joins the reasons into one line of prose and clamps it to two lines. The
  // full text stays reachable -- on hover here, and unclamped on the task's own page,
  // which is where a reader who wants to act on it is going anyway. task-341.
  const summary = state.reasons.join(" · ");
  return (
    <div className={compact ? "space-y-1" : "space-y-2"}>
      <span className="inline-flex flex-wrap items-center gap-1">
        <StatusChip category={state.category} label={state.label} motion={state.motion} />
        {task.archived && <ArchivedTag />}
      </span>
      {compact
        ? summary !== "" && (
            <p className="line-clamp-2 break-words text-xs leading-5 text-dark-muted" title={summary}>
              {summary}
            </p>
          )
        : state.reasons.map((reason) => (
            <p className="break-words text-xs leading-5 text-dark-muted" key={reason}>{reason}</p>
          ))}
    </div>
  );
}
