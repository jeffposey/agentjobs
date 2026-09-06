import type { TaskRead } from "../api/types";

export const STATE_CLASSES = {
  actionable: "border-emerald-700 bg-emerald-950/40 text-emerald-300",
  blocked: "border-red-700 bg-red-950/40 text-red-300",
  cycle: "border-amber-600 bg-amber-950/40 text-amber-200",
  done: "border-slate-600 bg-slate-900 text-slate-300",
  flight: "border-blue-700 bg-blue-950/40 text-blue-300",
  // A task that was superseded, cancelled or duplicated is closed but was not
  // finished, and the difference is the whole point of recording an outcome. Its own
  // colour so a scan of the list catches it without reading every label.
  unfinished: "border-violet-700 bg-violet-950/40 text-violet-300",
  waiting: "border-dark-border bg-dark-bg text-dark-muted",
} as const;

export function dependencyState(task: TaskRead) {
  if ((task.needs_cycles?.length ?? 0) > 0) {
    return {
      kind: "cycle" as const,
      label: "Dependency data error",
      reasons: task.needs_cycles?.map((cycle) => `Cycle: ${cycle.join(" → ")}`) ?? [],
    };
  }
  if (task.lifecycle === "closed") {
    // `display_status` is the backend's canonical label -- Completed, Superseded,
    // Cancelled, Duplicate, each with "(archived)" when it applies. Collapsing all
    // four to "Done" told a reviewer that a superseded task had been finished, which
    // is the opposite of what its record says. Derive nothing here; show that.
    const finished = (task.outcome ?? "completed") === "completed";
    return {
      kind: finished ? ("done" as const) : ("unfinished" as const),
      label: task.display_status,
      reasons: [],
    };
  }
  if (task.ball === "agent" && task.ball_reason === "hold") {
    // Before this, a held task fell through to `lifecycle === "active"` and read "In
    // flight" -- the badge asserting work was underway on the one task a human had
    // deliberately stopped. It sits above the blocked and dependency branches because
    // a hold outranks them: what a reader needs to know is that nothing will move
    // until a person releases it, whatever else is also true.
    return {
      kind: "waiting" as const,
      label: task.display_status,
      reasons: task.ball_prompt ? [task.ball_prompt] : [],
    };
  }
  if ((task.unmet_needs?.length ?? 0) > 0) {
    return {
      kind: "blocked" as const,
      label: "Blocked",
      reasons: task.unmet_needs?.map((reason) => `Waiting for ${reason}`) ?? [],
    };
  }
  if (task.ball === "external") {
    return {
      kind: "blocked" as const,
      label: "Blocked",
      reasons: [task.ball_prompt || "Waiting for an external dependency."],
    };
  }
  if (task.ball === "human") {
    return {
      kind: "waiting" as const,
      label: task.display_status,
      reasons: task.ball_prompt ? [task.ball_prompt] : [],
    };
  }
  if (task.lifecycle === "active") {
    return { kind: "flight" as const, label: "In flight", reasons: [] };
  }
  if ((task.open_children_count ?? 0) > 0) {
    const count = task.open_children_count ?? 0;
    return {
      kind: "waiting" as const,
      label: "Waiting on sub-tasks",
      // Not "must finish first": since task-164 an epic can be claimed, and what that
      // hands you is the supervisor's seat rather than the children's work.
      reasons: [
        `${count} open sub-task${count === 1 ? "" : "s"} to finish. ` +
          "Claim it to supervise them — a session per child, not the work itself.",
      ],
    };
  }
  if (task.actionable) {
    return { kind: "actionable" as const, label: "Actionable now", reasons: [] };
  }
  return {
    kind: "waiting" as const,
    label: task.display_status,
    reasons: task.ball_prompt ? [task.ball_prompt] : [],
  };
}

export function DependencyState({ task, compact = false }: { task: TaskRead; compact?: boolean }) {
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
      <span className={`inline-flex rounded border px-2 py-1 text-xs font-medium ${STATE_CLASSES[state.kind]}`}>
        {state.label}
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
