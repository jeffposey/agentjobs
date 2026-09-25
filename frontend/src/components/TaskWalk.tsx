import { Link } from "react-router-dom";

import type { EpicWalkView } from "../api/types";
import { StatusChip, WALK_STATES, categoryMotion, runMotion } from "./StatusChip";
import { walkCountsSentence, walkState } from "./SlotBoard";

/**
 * The open walk supervising this task, if any (task-591).
 *
 * The card's detail -- counts, the children in flight, the child waited on -- is read
 * off the machine-wide live-runs answer, `walks` on `GET /api/runs/live` (task-523). The
 * app shell polls that query for its nav badge on every page, so the task page reads it
 * from the shared cache and adds no loop of its own. The chip's "Walking" and the
 * Dispatch withhold come off the task read's own `live_walk` instead, so they arrive
 * with the page.
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
 * The card's badge. "Running" while the walk is taking off, as the Landing panel says
 * "Running" under a task chip reading "Landing" -- the header already says "Walking", and
 * saying it twice in one screen is repetition rather than information. Once grounded it
 * says which kind, because "waiting" and "grounded" are the two states a reader must not
 * confuse (task-467).
 */
export function walkBadge(walk: EpicWalkView): string {
  return walk.grounded ? walkState(walk).badge : "Running";
}

/**
 * The walk, as a panel on the epic's own page, in the shape of the Landing panel: a
 * moving badge, a heading, a line of what it is doing, and the children to follow.
 */
export function TaskWalkPanel({ walk, taskPath }: {
  walk: EpicWalkView;
  taskPath: (taskId: string) => string;
}) {
  const state = walkState(walk);
  const inFlight = walk.in_flight_task_ids ?? [];
  // The running badge moves as a working run's does; a grounded one takes its state's
  // own motion, which is a flash for one waiting on a person and nothing for a wait.
  const motion = walk.grounded ? categoryMotion(state.category) : runMotion("working");
  return (
    <section
      aria-label="Epic walk"
      data-testid="task-walk"
      data-grounded={walk.grounded ? "true" : "false"}
      className="space-y-3 rounded-xl border-2 border-blue-700/50 bg-blue-950/30 p-4 @min-[768px]:p-6"
    >
      <div className="flex flex-wrap items-center gap-3">
        <StatusChip
          testId="task-walk-badge"
          category={walk.grounded ? state.category : WALK_STATES.walking.category}
          label={walkBadge(walk)}
          motion={motion}
        />
        <h2 className="text-lg font-semibold text-blue-200">Being walked</h2>
        {walk.started_at && (
          <span className="text-sm text-dark-muted">
            since {new Date(walk.started_at).toLocaleString()}
          </span>
        )}
      </div>
      <p className="text-sm text-dark-muted">
        The server is supervising this epic and starts its children on its own, so there
        is nothing to dispatch here.
      </p>
      <p data-testid="task-walk-state" className="text-sm text-blue-100">
        {state.sentence.charAt(0).toUpperCase() + state.sentence.slice(1)}
      </p>
      <p data-testid="task-walk-counts" className="text-sm">
        {walkCountsSentence(walk)}
      </p>
      {inFlight.length > 0 && (
        <p data-testid="task-walk-in-flight" className="text-sm">
          <span className="text-dark-muted">Children running now: </span>
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
