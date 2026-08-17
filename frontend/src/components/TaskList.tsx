import { useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import type { BrokenTaskFile, TaskRead } from "../api/types";
import { BrokenFiles } from "./BrokenFiles";
import { DependencyState } from "./DependencyState";
import { ResponsiveCell, ResponsiveTable, ResponsiveTableRow } from "./ResponsiveTable";

type TaskRow = {
  task: TaskRead;
  depth: number;
  ancestors: Array<string>;
  childCount: number;
  openChildren: number;
};

const STATUS_FILTERS = new Set(["all", "open", "draft", "ready", "active", "human", "external", "closed"]);
const PRIORITY_FILTERS = new Set(["all", "critical", "high", "medium", "low"]);
const SCOPE_FILTERS = new Set(["all", "project", "test"]);
const PRIORITY_RANK: Record<string, number> = { critical: 0, high: 1, medium: 2, low: 3 };
const PRIORITY_CLASSES: Record<string, string> = {
  critical: "bg-red-900 text-red-200",
  high: "bg-orange-900 text-orange-200",
  medium: "bg-yellow-900 text-yellow-200",
  low: "bg-slate-700 text-slate-200",
};

export function buildTaskRows(tasks: Array<TaskRead>): Array<TaskRow> {
  const ordered = [...tasks].sort((left, right) => {
    const timeDifference = new Date(right.updated).getTime() - new Date(left.updated).getTime();
    return timeDifference || (PRIORITY_RANK[left.priority ?? "medium"] ?? 2) - (PRIORITY_RANK[right.priority ?? "medium"] ?? 2);
  });
  const byId = new Map(ordered.map((task) => [task.id, task]));
  const children = new Map<string | null, Array<TaskRead>>();

  for (const task of ordered) {
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
  for (const task of ordered) {
    if (!drawn.has(task.id)) {
      rows.push({ task, depth: 0, ancestors: [], childCount: 0, openChildren: 0 });
    }
  }
  return rows;
}

function matchesTask(task: TaskRead, search: string, status: string, priority: string, scope: string) {
  const term = search.trim().toLowerCase();
  const titleMatches = term === "" || task.title.toLowerCase().includes(term);
  const statusMatches = status === "all"
    || (status === "open" && task.lifecycle !== "closed")
    || task.lifecycle === status
    || task.ball === status;
  const priorityMatches = priority === "all" || task.priority === priority;
  const tags = task.tags ?? [];
  const isTest = tags.includes("test") || tags.includes("example");
  const scopeMatches = scope === "all" || (scope === "test" ? isTest : !isTest);
  return titleMatches && statusMatches && priorityMatches && scopeMatches;
}

function filterValue(params: URLSearchParams, key: string, allowed: Set<string>, fallback: string) {
  const candidate = params.get(key) ?? fallback;
  return allowed.has(candidate) ? candidate : fallback;
}

function taskPath(projectId: string, taskId: string) {
  return `/p/${encodeURIComponent(projectId)}/tasks/${encodeURIComponent(taskId)}`;
}

export function TaskList({
  tasks,
  brokenFiles,
  projectId,
}: {
  tasks: Array<TaskRead>;
  brokenFiles: Array<BrokenTaskFile>;
  projectId: string;
}) {
  const [params, setParams] = useSearchParams();
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());
  const search = params.get("q") ?? "";
  const status = filterValue(params, "status", STATUS_FILTERS, "open");
  const priority = filterValue(params, "priority", PRIORITY_FILTERS, "all");
  const scope = filterValue(params, "scope", SCOPE_FILTERS, "all");
  const flattened = search.trim() !== "" || status !== "all" || priority !== "all" || scope !== "all";
  const rows = useMemo(() => buildTaskRows(tasks), [tasks]);
  const visibleRows = rows.filter((row) => {
    if (!matchesTask(row.task, search, status, priority, scope)) return false;
    return flattened || row.ancestors.every((ancestor) => expanded.has(ancestor));
  });

  const updateParam = (key: string, value: string, fallback: string) => {
    const next = new URLSearchParams(params);
    if (value === fallback) next.delete(key);
    else next.set(key, value);
    setParams(next, { replace: true });
  };

  const toggle = (id: string) => {
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  return (
    <div className="space-y-6">
      <BrokenFiles files={brokenFiles} />
      <section className="rounded-lg border border-dark-border bg-dark-surface p-4" aria-label="Task filters">
        <div className="grid gap-3 min-[820px]:grid-cols-[minmax(16rem,1fr)_repeat(3,minmax(9rem,auto))]">
          <label className="sr-only" htmlFor="task-search">Search tasks</label>
          <input
            id="task-search"
            type="search"
            value={search}
            onChange={(event) => updateParam("q", event.target.value, "")}
            placeholder="Search tasks..."
            className="touch-target w-full rounded-lg border border-dark-border bg-dark-bg px-4 text-dark-text focus:border-blue-500 focus:outline-none"
          />
          <label className="sr-only" htmlFor="status-filter">Status</label>
          <select id="status-filter" aria-label="Status" value={status} onChange={(event) => updateParam("status", event.target.value, "open")} className="touch-target w-full rounded-lg border border-dark-border bg-dark-bg px-3">
            <option value="open">Open (not closed)</option><option value="all">All Status</option><option value="draft">Draft</option><option value="ready">Ready</option><option value="active">Active</option><option value="human">Needs Human</option><option value="external">Blocked</option><option value="closed">Closed</option>
          </select>
          <label className="sr-only" htmlFor="priority-filter">Priority</label>
          <select id="priority-filter" aria-label="Priority" value={priority} onChange={(event) => updateParam("priority", event.target.value, "all")} className="touch-target w-full rounded-lg border border-dark-border bg-dark-bg px-3">
            <option value="all">All Priorities</option><option value="critical">Critical</option><option value="high">High</option><option value="medium">Medium</option><option value="low">Low</option>
          </select>
          <label className="sr-only" htmlFor="scope-filter">Scope</label>
          <select id="scope-filter" aria-label="Scope" value={scope} onChange={(event) => updateParam("scope", event.target.value, "all")} className="touch-target w-full rounded-lg border border-dark-border bg-dark-bg px-3">
            <option value="all">All Tasks</option><option value="project">Project Tasks</option><option value="test">Test/Examples</option>
          </select>
        </div>
      </section>

      <section className="overflow-hidden rounded-lg border border-dark-border bg-dark-surface" aria-label="Tasks">
        <ResponsiveTable>
          <thead><tr><th scope="col">Task</th><th scope="col">Status</th><th scope="col">Priority</th><th scope="col">Assigned</th><th scope="col">Updated</th></tr></thead>
          <tbody>
            {visibleRows.map((row) => (
              <ResponsiveTableRow key={row.task.id}>
                <ResponsiveCell label="Task" style={!flattened ? { paddingLeft: `${0.5 + row.depth * 1.5}rem` } : undefined}>
                  <Link to={taskPath(projectId, row.task.id)} className="touch-target block overflow-hidden">
                    <span className="block font-mono text-xs text-blue-400">{row.task.id}</span>
                    <span className="block truncate font-medium text-dark-text">{row.task.title}</span>
                    <span className="block text-xs text-dark-muted">
                      {row.task.category}
                      {flattened && row.ancestors.length > 0 ? ` · part of ${row.ancestors.at(-1)}` : ""}
                    </span>
                  </Link>
                  {!flattened && row.childCount > 0 && (
                    <button type="button" aria-expanded={expanded.has(row.task.id)} onClick={() => toggle(row.task.id)} className="touch-target text-xs text-blue-400 hover:text-blue-300">
                      {expanded.has(row.task.id) ? "▾" : "▸"} {row.childCount} sub-task{row.childCount === 1 ? "" : "s"}{row.openChildren ? `, ${row.openChildren} open` : ""}
                    </button>
                  )}
                </ResponsiveCell>
                <ResponsiveCell label="Status"><DependencyState task={row.task} compact /></ResponsiveCell>
                <ResponsiveCell label="Priority"><span className={`rounded px-2 py-1 text-xs ${PRIORITY_CLASSES[row.task.priority ?? "medium"]}`}>{row.task.priority ?? "medium"}</span></ResponsiveCell>
                <ResponsiveCell label="Assigned" className="text-sm">{row.task.assignment?.owner ?? "—"}</ResponsiveCell>
                <ResponsiveCell label="Updated" className="text-sm text-dark-muted"><time dateTime={row.task.updated}>{new Date(row.task.updated).toLocaleString([], { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" })}</time></ResponsiveCell>
              </ResponsiveTableRow>
            ))}
          </tbody>
        </ResponsiveTable>
        {tasks.length === 0 && <p className="p-6 text-sm text-dark-muted">No tasks have been created yet.</p>}
        {tasks.length > 0 && visibleRows.length === 0 && <p className="p-6 text-sm text-dark-muted">No tasks match these filters.</p>}
      </section>
    </div>
  );
}
