import { Link } from "react-router-dom";

import type { EpicWalkView } from "../api/types";
import { StatusChip, categoryMotion } from "./StatusChip";
import { walkCountsSentence, walkState } from "./SlotBoard";

/**
 * The open walk supervising this task, if any (task-591).
 *
 * Read off the machine-wide live-runs answer rather than the task record, because that
 * is where a walk lives: `walks` on `GET /api/runs/live` (task-523). The app shell polls
 * that query for its nav badge on every page, so the task page reads it from the shared
 * cache and adds no loop of its own.
 */
export function walkForTask(
  walks: EpicWalkView[] | null | undefined,
  projectId: string,
  taskId: string,
): EpicWalkView | null {
  return (
    (walks ?? []).find(
      (walk) => walk.project_id === projectId && walk.parent_task_id === taskId,
    ) ?? null
  );
}

/**
 * The header chip's word. Prefixed where the bare vocabulary word would read as the
 * task's own status: "Waiting" beside "Working" says the task is waiting, when it is the
 * walk that is.
 */
export function walkChipLabel(walk: EpicWalkView): string {
  const state = walkState(walk);
  return walk.grounded ? `Walk ${state.badge.toLowerCase()}` : state.badge;
}

/** The chip beside the status chip in the task header. */
export function TaskWalkChip({ walk }: { walk: EpicWalkView }) {
  const state = walkState(walk);
  return (
    <StatusChip
      testId="task-walk-chip"
      category={state.category}
      label={walkChipLabel(walk)}
      title={`This epic is being walked: ${state.sentence}`}
      motion={categoryMotion(state.category)}
    />
  );
}

/**
 * The walk, as a card on the epic's own page: what the dashboard's walk rail says about
 * it, plus the children in the air, since this page is where somebody goes to follow one.
 */
export function TaskWalkPanel({ walk, taskPath }: {
  walk: EpicWalkView;
  taskPath: (taskId: string) => string;
}) {
  const state = walkState(walk);
  const inFlight = walk.in_flight_task_ids ?? [];
  return (
    <section
      aria-label="Epic walk"
      data-testid="task-walk"
      data-grounded={walk.grounded ? "true" : "false"}
      className="space-y-2 rounded-xl border border-dashed border-dark-border bg-dark-surface p-4"
    >
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <h2 className="text-lg font-semibold">Being walked</h2>
        <StatusChip
          category={state.category}
          label={state.badge}
          motion={categoryMotion(state.category)}
        />
        {walk.started_at && (
          <span className="text-xs text-dark-muted">
            since {new Date(walk.started_at).toLocaleString()}
          </span>
        )}
      </div>
      <p className="text-sm text-dark-muted">
        The server is supervising this epic and starts its children on its own. Nothing
        needs dispatching here.
      </p>
      <p data-testid="task-walk-counts" className="text-sm">
        {walkCountsSentence(walk)}
      </p>
      <p data-testid="task-walk-state" className="text-sm">
        {state.sentence.charAt(0).toUpperCase() + state.sentence.slice(1)}
      </p>
      {inFlight.length > 0 && (
        <p data-testid="task-walk-in-flight" className="text-sm">
          <span className="text-dark-muted">In flight: </span>
          {inFlight.map((id, index) => (
            <span key={id}>
              {index > 0 && ", "}
              <Link className="font-mono text-blue-300 hover:underline" to={taskPath(id)}>
                {id}
              </Link>
            </span>
          ))}
        </p>
      )}
      {walk.waiting_on_task_id && (
        <p data-testid="task-walk-waiting-on" className="text-sm">
          <span className="text-dark-muted">Waiting on: </span>
          <Link
            className="hover:underline"
            to={walk.waiting_on_task_url || taskPath(walk.waiting_on_task_id)}
          >
            <span className="font-mono text-blue-300">{walk.waiting_on_task_id}</span>{" "}
            <span>{walk.waiting_on_task_title}</span>
          </Link>
        </p>
      )}
    </section>
  );
}
