import type { TaskRead } from "../api/types";

/**
 * Reordering the backlog, as arithmetic the browser can do without a server.
 *
 * Two jobs, both pure so both are testable without a DOM:
 *
 * * **Where a gesture lands.** `Alt+Down` is not "swap with the row below"; it is
 *   "stand behind the next task in your band". Those differ the moment the list is
 *   filtered or grouped by parent, because the row below may be in another band or
 *   may not be rendered at all. A position is a property of a band, so every step here
 *   is computed over the band and never over the view.
 * * **What the screen shows before the server answers.** `applyMove` predicts the
 *   result so a reorder feels immediate. It is a prediction and nothing more: the
 *   server picks the actual number under the queue lock, and the refetch replaces this
 *   the moment it lands. It is never a sort — it moves exactly one task and leaves
 *   everything the server ordered in the order the server ordered it.
 */

export type QueueMove =
  | { top: true }
  | { bottom: true }
  | { before: string }
  | { after: string };

export type StepDirection = "up" | "down" | "top" | "bottom";

/** Whether this task has a place in line that a person can change. */
export function isInQueue(task: TaskRead): boolean {
  return task.lifecycle !== "closed" && task.queue_position !== null && task.queue_position !== undefined;
}

export function bandOf(task: TaskRead): string {
  return task.priority ?? "medium";
}

/**
 * One band, in the order the server sent it.
 *
 * Filtering preserves order, so this does not sort and must not: the input arrives in
 * `(band, queue_position)` and re-deriving that here would be a second opinion about
 * the queue living in the browser, which is the thing task-207 deleted.
 */
export function bandMembers(tasks: Array<TaskRead>, band: string): Array<TaskRead> {
  return tasks.filter((task) => isInQueue(task) && bandOf(task) === band);
}

/**
 * The single `queue-move` a step gesture means, or null when it would not move.
 *
 * Null rather than a no-op request: firing a move that lands a task exactly where it
 * already is writes a `queue_move` log entry recording a decision nobody made.
 */
export function stepMove(
  tasks: Array<TaskRead>,
  taskId: string,
  direction: StepDirection,
): QueueMove | null {
  const task = tasks.find((candidate) => candidate.id === taskId);
  if (!task || !isInQueue(task)) return null;
  const band = bandMembers(tasks, bandOf(task));
  const index = band.findIndex((candidate) => candidate.id === taskId);
  if (index < 0) return null;

  const ahead = band[index - 1];
  const behind = band[index + 1];
  switch (direction) {
    case "up":
      return ahead ? { before: ahead.id } : null;
    case "down":
      return behind ? { after: behind.id } : null;
    case "top":
      return ahead ? { top: true } : null;
    case "bottom":
      return behind ? { bottom: true } : null;
  }
}

/** Where in the band, after removing the mover, this placement puts it. */
function insertionIndex(rest: Array<TaskRead>, move: QueueMove): number {
  if ("top" in move) return 0;
  if ("bottom" in move) return rest.length;
  if ("before" in move) {
    const at = rest.findIndex((task) => task.id === move.before);
    return at < 0 ? rest.length : at;
  }
  const at = rest.findIndex((task) => task.id === move.after);
  return at < 0 ? rest.length : at + 1;
}

/**
 * The list as it will look once the move lands — one task moved, nobody else touched.
 *
 * The band's existing position numbers are dealt back out in the new order rather than
 * invented, so the position column stays plausible for the second before the refetch
 * arrives. The server does not choose these numbers: sparse numbering means it gives
 * the mover a number between its new neighbours and leaves every other file alone.
 * Predicting that exactly would mean reimplementing `plan_insertion` in TypeScript, so
 * this predicts the *order* faithfully and the numbers only well enough to read.
 */
export function applyMove(
  tasks: Array<TaskRead>,
  taskId: string,
  move: QueueMove,
): Array<TaskRead> {
  const task = tasks.find((candidate) => candidate.id === taskId);
  if (!task || !isInQueue(task)) return tasks;
  const band = bandOf(task);
  const members = bandMembers(tasks, band);
  const rest = members.filter((candidate) => candidate.id !== taskId);
  const reordered = [...rest];
  reordered.splice(insertionIndex(rest, move), 0, task);

  const positions = members.map((member) => member.queue_position);
  const queue = reordered.map((member, index) => ({
    ...member,
    queue_position: positions[index] ?? member.queue_position,
  }));
  // Rebuilt by walking the original list and handing back band members in their new
  // order as each band slot comes up, so tasks outside the band keep their places.
  let next = 0;
  return tasks.map((candidate) =>
    isInQueue(candidate) && bandOf(candidate) === band
      ? queue[next++] ?? candidate
      : candidate,
  );
}

/** What just happened, as a sentence for the live region. */
export function describeMove(
  tasks: Array<TaskRead>,
  taskId: string,
  move: QueueMove,
): string {
  const task = tasks.find((candidate) => candidate.id === taskId);
  const band = task ? bandOf(task) : "";
  if ("top" in move) return `${taskId} moved to the top of the ${band} band.`;
  if ("bottom" in move) return `${taskId} moved to the bottom of the ${band} band.`;
  if ("before" in move) return `${taskId} moved ahead of ${move.before} in the ${band} band.`;
  return `${taskId} moved behind ${move.after} in the ${band} band.`;
}
