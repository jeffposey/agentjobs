import type { TaskRead } from "../api/types";

/**
 * The task list as a tree: rows, what is folded away, and where an arrow key lands.
 *
 * Everything here is pure and DOM-free, for the same reason `queueOrder.ts` is: the
 * hard parts of a tree are arithmetic over a list, and arithmetic is worth testing
 * without a renderer. `queueOrder.ts` answers *where a task belongs in the queue*;
 * this answers *which rows a reader can see and which one an arrow key moves to*. The
 * two never meet -- a step gesture is computed over the band and never over the rows
 * on screen, which is precisely why folding a parent cannot change what Alt+Up does.
 */

export type TaskRow = {
  task: TaskRead;
  depth: number;
  /** Every ancestor, outermost first. Empty for a row drawn at the root. */
  ancestors: Array<string>;
  childCount: number;
  openChildren: number;
};

/**
 * Rows in the order the server sent them, grouped under their parents.
 *
 * **There is no sort here, deliberately.** Until task-207 this sorted by `updated`
 * descending and consulted priority only to break a tie, so the list a human read was
 * ordered by one rule while the scheduler answered by another — and neither rule had
 * been chosen by anybody. `manager.list_tasks` now settles the order in
 * `(band, queue_position)`, and the client's job is to not undo that.
 *
 * The parent grouping is a regrouping, not a re-sort: siblings keep the order they
 * arrived in, so within any one group the queue's order survives. It does move a child
 * away from its own band's run of rows, which is why the position column exists and
 * why every reorder gesture is computed over the band rather than over the rows on
 * screen.
 *
 * A task whose parent is not in `tasks` is drawn at the root rather than dropped. That
 * is what makes it safe to hand this a *filtered* list: the sidebar tree groups at
 * every filter setting, and a child whose parent is closed simply becomes a root row.
 */
export function buildTaskRows(tasks: Array<TaskRead>): Array<TaskRow> {
  const byId = new Map(tasks.map((task) => [task.id, task]));
  const children = new Map<string | null, Array<TaskRead>>();

  for (const task of tasks) {
    const parent = task.parent && byId.has(task.parent) ? task.parent : null;
    children.set(parent, [...(children.get(parent) ?? []), task]);
  }

  const rows: Array<TaskRow> = [];
  const drawn = new Set<string>();
  const walk = (task: TaskRead, ancestors: Array<string>) => {
    if (drawn.has(task.id)) return;
    const kids = children.get(task.id) ?? [];
    rows.push({
      task,
      depth: ancestors.length,
      ancestors,
      childCount: kids.length,
      openChildren: kids.filter((child) => child.lifecycle !== "closed").length,
    });
    drawn.add(task.id);
    for (const child of kids) {
      if (child.id === task.id || ancestors.includes(child.id)) continue;
      walk(child, [...ancestors, task.id]);
    }
  };

  for (const root of children.get(null) ?? []) walk(root, []);

  // Cycles have no root. Append every undrawn record flat so malformed ancestry can
  // never make a task disappear from the list.
  for (const task of tasks) {
    if (!drawn.has(task.id)) {
      rows.push({ task, depth: 0, ancestors: [], childCount: 0, openChildren: 0 });
    }
  }
  return rows;
}

/** How much a fold is hiding, so a collapsed parent can say so rather than imply it. */
export type HiddenCount = { total: number; open: number };

/**
 * Every descendant of every row, counted once.
 *
 * Descendants rather than children: folding task-235 hides its grandchildren too, and
 * a badge reading "3 sub-tasks" over eleven hidden rows is the silent hiding this is
 * here to prevent.
 */
export function descendantCounts(rows: Array<TaskRow>): Map<string, HiddenCount> {
  const counts = new Map<string, HiddenCount>();
  for (const row of rows) {
    for (const ancestor of row.ancestors) {
      const running = counts.get(ancestor) ?? { total: 0, open: 0 };
      running.total += 1;
      if (row.task.lifecycle !== "closed") running.open += 1;
      counts.set(ancestor, running);
    }
  }
  return counts;
}

/**
 * The rows a reader can actually see: everything with no folded ancestor.
 *
 * A predicate rather than a set, because what is stored is the *exceptions* to the
 * default fold state -- see `readCollapsed` -- so "is this one folded" is a question
 * about the set and the default together, and only the caller knows the default.
 */
export function unfoldedRows(
  rows: Array<TaskRow>,
  isFolded: (taskId: string) => boolean,
): Array<TaskRow> {
  return rows.filter((row) => !row.ancestors.some(isFolded));
}

/**
 * The row an Up or Down arrow lands on, or null at either end.
 *
 * Over the *visible* rows, unlike every gesture in `queueOrder.ts`. Moving the
 * selection is a claim about the screen -- a reader pressing Down means "the next row
 * I can see" -- where moving a task is a claim about its band.
 */
export function neighbourRow(
  visible: Array<TaskRow>,
  taskId: string,
  direction: 1 | -1,
): TaskRow | null {
  const index = visible.findIndex((row) => row.task.id === taskId);
  if (index < 0) return null;
  return visible[index + direction] ?? null;
}

/** Where localStorage keeps one project's folds. Per project, never global. */
export function collapseKey(projectId: string): string {
  return `agentjobs.tasks.collapsed.${projectId}`;
}

/**
 * The parents this browser recorded as *differing from the default* for a project.
 *
 * **Exceptions are stored, never the whole state.** With the default open -- which is
 * what `FOLDED_BY_DEFAULT` currently says -- this is the set of folded parents, so a
 * parent filed tomorrow is not in it, is therefore open, and nobody has to migrate a
 * stored list when the backlog grows. Flipping the default flips the meaning of the
 * set with it and the same property holds, which is why the default is one line.
 *
 * A browser with storage switched off, or a value some other tab wrote in a shape this
 * does not understand, yields an empty set rather than an exception: a fold is a
 * convenience, and losing one must never cost a reader their task list.
 */
export function readCollapsed(projectId: string): Set<string> {
  try {
    const raw = window.localStorage.getItem(collapseKey(projectId));
    if (!raw) return new Set();
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return new Set();
    return new Set(parsed.filter((entry): entry is string => typeof entry === "string"));
  } catch {
    return new Set();
  }
}

export function writeCollapsed(projectId: string, collapsed: Set<string>): void {
  try {
    window.localStorage.setItem(collapseKey(projectId), JSON.stringify([...collapsed]));
  } catch {
    // Storage full, or disabled. The fold still works for this session.
  }
}
