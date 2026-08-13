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

export function requireSupportedTaskSchemas(tasks: Task[]): Task[] {
  for (const task of tasks) {
    if (task.schema !== SUPPORTED_TASK_SCHEMA) {
      throw new UnsupportedTaskSchemaError(task.id, task.schema);
    }
  }

  return tasks;
}
