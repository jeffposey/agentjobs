import type { Task } from "./generated";

export const SUPPORTED_TASK_SCHEMA = 2;

export class UnsupportedTaskSchemaError extends Error {
  constructor(taskId: string, received: number | undefined) {
    super(
      `Task ${taskId} uses schema ${received ?? "missing"}; this client supports only schema ${SUPPORTED_TASK_SCHEMA}.`,
    );
    this.name = "UnsupportedTaskSchemaError";
  }
}

/** The stamp this checks, and all it needs: whole records and listing rows both carry it. */
type SchemaStamped = Pick<Task, "id" | "schema">;

export function requireSupportedTaskSchemas<T extends SchemaStamped>(tasks: T[]): T[] {
  for (const task of tasks) {
    if (task.schema !== SUPPORTED_TASK_SCHEMA) {
      throw new UnsupportedTaskSchemaError(task.id, task.schema);
    }
  }

  return tasks;
}
