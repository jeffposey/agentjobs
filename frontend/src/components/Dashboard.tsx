import { Link } from "react-router-dom";

import type { DashboardResponse, Task } from "../api/generated";
import { ResponsiveCell, ResponsiveTable, ResponsiveTableRow } from "./ResponsiveTable";

type DashboardProps = {
  dashboard: DashboardResponse;
  projectId: string;
};

const priorityClasses: Record<string, string> = {
  critical: "bg-red-900 text-red-200",
  high: "bg-orange-900 text-orange-200",
  medium: "bg-yellow-900 text-yellow-200",
  low: "bg-slate-700 text-slate-200",
};

function projectPath(projectId: string, path = "") {
  return `/p/${encodeURIComponent(projectId)}${path}`;
}

function truncate(text: string, limit: number) {
  const compact = text.replace(/\s+/g, " ").trim();
  return compact.length <= limit ? compact : `${compact.slice(0, limit - 1)}…`;
}

function Badge({ children, className = "" }: { children: React.ReactNode; className?: string }) {
  return (
    <span className={`whitespace-nowrap rounded px-2 py-1 text-xs ${className}`}>
      {children}
    </span>
  );
}

function TaskCard({ task, projectId }: { task: Task; projectId: string }) {
  return (
    <Link
      to={projectPath(projectId, `/tasks/${encodeURIComponent(task.id)}`)}
      className="touch-target block overflow-hidden rounded-lg border border-dark-border bg-dark-surface p-4 transition hover:border-blue-500"
    >
      <div className="flex flex-col items-start justify-between gap-4 min-[820px]:flex-row min-[820px]:items-center">
        <div className="min-w-0">
          <h3 className="truncate text-lg font-medium">{task.title}</h3>
          <p className="mt-1 truncate text-sm text-dark-muted">{truncate(task.spec.summary, 120)}</p>
        </div>
        <div className="flex flex-shrink-0 flex-wrap items-center gap-2">
          <Badge className={priorityClasses[task.priority ?? "medium"] ?? priorityClasses.medium}>
            {task.priority ?? "medium"}
          </Badge>
          <Badge className="bg-dark-bg text-dark-muted">{task.display_status}</Badge>
        </div>
      </div>
    </Link>
  );
}

function CreateTaskUnavailable({ id }: { id: string }) {
  return (
    <div className="mobile-action-row flex items-center gap-3">
      <button
        type="button"
        disabled
        aria-describedby={id}
        className="touch-target cursor-not-allowed rounded-md border border-dark-border bg-dark-bg px-3 py-2 text-sm font-medium text-dark-muted opacity-60"
      >
        Create task
      </button>
      <span id={id} className="text-xs text-dark-muted">
        Not yet implemented — arrives with the browser UI (task-090).
      </span>
    </div>
  );
}

function NextAction({ dashboard, projectId }: DashboardProps) {
  const base = projectPath(projectId);

  switch (dashboard.next_action) {
    case "blocked":
      return (
        <section
          data-testid="next-action"
          className="rounded-lg border-2 border-red-500/50 bg-gradient-to-r from-red-900/20 to-orange-900/20 p-6"
        >
          <div className="flex items-start gap-4">
            <div className="text-4xl" aria-hidden="true">🔔</div>
            <div className="flex-1">
              <h2 className="mb-2 text-xl font-bold text-red-300">
                {dashboard.waiting_tasks.length} {dashboard.waiting_tasks.length === 1 ? "Task" : "Tasks"} Blocked on You
              </h2>
              <p className="mb-4 text-dark-muted">Work has stopped on these until you act.</p>
              <div className="space-y-2">
                {dashboard.waiting_tasks.slice(0, 3).map((task) => (
                  <Link
                    key={task.id}
                    to={`${base}/tasks/${encodeURIComponent(task.id)}`}
                    className="block rounded-lg border border-dark-border bg-dark-surface p-4 transition hover:bg-dark-border"
                  >
                    <div className="flex items-start justify-between gap-4">
                      <div className="flex-1">
                        <div className="font-mono text-xs text-blue-400">{task.id}</div>
                        <h3 className="font-medium text-dark-text">{task.title}</h3>
                        <p className="mt-1 text-sm text-dark-muted">
                          {truncate(task.ball_prompt ?? task.spec.summary, 160)}
                        </p>
                      </div>
                      <Badge className="bg-red-900 text-red-200">{task.display_status}</Badge>
                    </div>
                  </Link>
                ))}
                {dashboard.waiting_tasks.length > 3 && (
                  <Link to={`${base}/tasks?status=human`} className="block pt-2 text-center text-sm text-blue-400 hover:text-blue-300">
                    View all {dashboard.waiting_tasks.length} waiting tasks →
                  </Link>
                )}
              </div>
            </div>
          </div>
        </section>
      );
    case "backlog":
      return (
        <section data-testid="next-action" className="rounded-lg border border-dark-border bg-dark-surface p-6">
          <div className="mb-1 flex items-baseline justify-between gap-4">
            <h2 className="text-sm font-medium text-dark-text">
              Backlog awaiting your input <span className="font-normal text-dark-muted">({dashboard.backlog_tasks.length})</span>
            </h2>
            <Link to={`${base}/tasks?status=draft`} className="touch-target text-xs text-blue-400 hover:text-blue-300">All drafts →</Link>
          </div>
          <p className="mb-4 text-xs text-dark-muted">
            Nothing is blocked by these. They are drafts that need a decision before they become work.
          </p>
          <ResponsiveTable aria-label="Backlog awaiting your input">
            <thead>
              <tr>
                <th scope="col">Task</th>
                <th scope="col">Title</th>
                <th scope="col">Reason</th>
              </tr>
            </thead>
            <tbody>
              {dashboard.backlog_tasks.map((task) => (
                <ResponsiveTableRow key={task.id}>
                  <ResponsiveCell label="Task">
                    <Link to={`${base}/tasks/${encodeURIComponent(task.id)}`} className="touch-target font-mono text-xs text-blue-400">
                      {task.id}
                    </Link>
                  </ResponsiveCell>
                  <ResponsiveCell label="Title" className="text-sm text-dark-text">{task.title}</ResponsiveCell>
                  <ResponsiveCell label="Reason" className="text-xs text-dark-muted">{task.ball_reason}</ResponsiveCell>
                </ResponsiveTableRow>
              ))}
            </tbody>
          </ResponsiveTable>
        </section>
      );
    case "next_up": {
      const task = dashboard.next_task;
      if (!task) return null;
      return (
        <section data-testid="next-action" className="rounded-lg border border-dark-border bg-dark-surface p-6">
          <div className="mb-3 flex items-baseline justify-between gap-4">
            <h2 className="text-sm font-medium text-dark-text">Next up</h2>
            <Link to={`${base}/tasks?status=ready`} className="touch-target text-xs text-blue-400 hover:text-blue-300">All ready tasks →</Link>
          </div>
          <Link to={`${base}/tasks/${encodeURIComponent(task.id)}`} className="block rounded-lg border border-dark-border bg-dark-bg p-4 transition hover:bg-dark-border">
            <div className="flex items-start justify-between gap-4">
              <div className="flex-1">
                <div className="font-mono text-xs text-blue-400">{task.id}</div>
                <h3 className="font-medium text-dark-text">{task.title}</h3>
                <p className="mt-1 text-sm text-dark-muted">{truncate(task.spec.summary, 160)}</p>
              </div>
              <Badge className="bg-dark-surface text-dark-muted">{task.priority}</Badge>
            </div>
          </Link>
        </section>
      );
    }
    case "nothing_claimable":
      return (
        <section data-testid="next-action" className="rounded-lg border border-dark-border bg-dark-surface p-6">
          <h2 className="mb-1 text-sm font-medium text-dark-text">Nothing claimable right now</h2>
          <p className="mb-4 text-xs text-dark-muted">
            Every open task is waiting on a dependency, or is an umbrella finished by its children. Adding work is the useful move.
          </p>
          <CreateTaskUnavailable id="create-task-unavailable" />
        </section>
      );
    case "empty_project":
      return (
        <section data-testid="next-action" className="rounded-lg border-2 border-blue-500/30 bg-gradient-to-r from-blue-900/20 to-purple-900/20 p-6">
          <div className="flex items-start gap-4">
            <div className="text-4xl" aria-hidden="true">🚀</div>
            <div className="flex-1">
              <h2 className="mb-2 text-xl font-bold text-blue-300">Getting Started with AgentJobs</h2>
              <p className="mb-4 text-dark-muted">No tasks yet. Here&apos;s how to get rolling:</p>
              <div className="space-y-3 text-sm">
                <div>
                  <strong className="text-dark-text">For Humans:</strong>
                  <div className="mt-1"><CreateTaskUnavailable id="create-task-unavailable-empty" /></div>
                </div>
                <div>
                  <strong className="text-dark-text">For AI Agents:</strong>
                  <pre className="mt-1 overflow-x-auto rounded bg-dark-bg p-2 text-xs">{`from agentjobs import TaskClient
client = TaskClient()
task = client.get_next_task()
client.claim_task(task.id, agent="agent-name")`}</pre>
                </div>
                <div className="mt-4"><a href="/docs" className="text-blue-400 underline hover:text-blue-300">View Full Documentation →</a></div>
              </div>
            </div>
          </div>
        </section>
      );
  }
}

function BrokenFiles({ files }: { files: DashboardResponse["broken_files"] }) {
  if (files.length === 0) return null;
  return (
    <section className="rounded-lg border-2 border-red-600/60 bg-red-950/40 p-4" aria-label="Unreadable task files">
      <h2 className="flex items-center gap-2 font-semibold text-red-300">
        <span aria-hidden="true">⚠️</span> {files.length} task file{files.length === 1 ? "" : "s"} could not be loaded
      </h2>
      <ul className="mt-2 space-y-1 text-sm">
        {files.map((file) => <li key={file.path}><span className="font-mono text-red-300">{file.filename}</span><span className="text-dark-muted"> — {file.reason}</span></li>)}
      </ul>
      <p className="mt-2 text-xs text-dark-muted">These are not shown below. Fix the file, or the task stays invisible.</p>
    </section>
  );
}

export function Dashboard({ dashboard, projectId }: DashboardProps) {
  const statTiles = [
    ["Blocked on You", dashboard.stats.waiting_for_human, "text-orange-400"],
    ["In Progress", dashboard.stats.in_progress, ""],
    ["Blocked", dashboard.stats.blocked, "text-red-400"],
    ["Completed", dashboard.stats.completed, "text-green-400"],
    ["Total", dashboard.stats.total, ""],
  ] as const;

  return (
    <div className="space-y-6">
      <BrokenFiles files={dashboard.broken_files} />
      <NextAction dashboard={dashboard} projectId={projectId} />
      <section className="grid grid-cols-1 gap-6 min-[820px]:grid-cols-5" aria-label="Task statistics">
        {statTiles.map(([label, count, className]) => (
          <div key={label} className="rounded-lg border border-dark-border bg-dark-surface p-6">
            <div className="text-sm text-dark-muted">{label}</div>
            <div className={`mt-2 text-3xl font-bold ${className}`}>{count}</div>
            {label === "Blocked on You" && (
              <Link to={projectPath(projectId, "/tasks?status=draft")} className="touch-target mt-1 text-xs text-dark-muted hover:text-blue-300">
                +{dashboard.stats.awaiting_input} in backlog
              </Link>
            )}
          </div>
        ))}
      </section>
      <section className="rounded-lg border border-dark-border bg-dark-surface">
        <div className="flex items-center justify-between border-b border-dark-border p-6">
          <h2 className="text-xl font-semibold">Active Tasks</h2>
          <Link to={projectPath(projectId, "/tasks")} className="touch-target text-sm text-blue-400 hover:text-blue-300">View all</Link>
        </div>
        <div className="space-y-4 p-4">
          {dashboard.active_tasks.length > 0 ? dashboard.active_tasks.map((task) => (
            <TaskCard key={task.id} task={task} projectId={projectId} />
          )) : (
            <div className="rounded-lg border border-dashed border-dark-border bg-dark-bg/40 p-6 text-center text-sm text-dark-muted">No active tasks right now. Enjoy the calm!</div>
          )}
        </div>
      </section>
      <section className="rounded-lg border border-dark-border bg-dark-surface">
        <div className="border-b border-dark-border p-6"><h2 className="text-xl font-semibold">Recent Updates</h2></div>
        <div className="divide-y divide-dark-border">
          {dashboard.recent_updates.length > 0 ? dashboard.recent_updates.map((update, index) => (
            <div className="p-4" key={`${update.task_id}-${update.timestamp}-${index}`}>
              <div className="flex flex-wrap items-center gap-2 text-sm text-dark-muted">
                <span className="font-medium text-dark-text">{update.task_title}</span><span>•</span>
                <time dateTime={update.timestamp}>{new Date(update.timestamp).toLocaleString([], { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" })}</time><span>•</span>
                <span>{update.author}</span>
              </div>
              <p className="mt-2 text-sm text-dark-muted">{update.summary}</p>
            </div>
          )) : <div className="p-6 text-sm text-dark-muted">No recent updates. Check back soon.</div>}
        </div>
      </section>
    </div>
  );
}
