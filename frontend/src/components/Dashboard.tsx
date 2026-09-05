import { Link } from "react-router-dom";

import type { DashboardResponse, TaskRead } from "../api/types";
import { BrokenFiles } from "./BrokenFiles";
import { DependencyState } from "./DependencyState";
import { QueueBroken } from "./QueueBroken";
import { ResponsiveCell, ResponsiveTable, ResponsiveTableRow } from "./ResponsiveTable";

type DashboardProps = {
  dashboard: DashboardResponse;
  projectId: string;
  /**
   * The "why this one" disclosure, supplied by the page rather than built here.
   *
   * It owns a query of its own, and this component is otherwise pure presentation
   * rendered straight from a response object in its tests. Same shape as the dispatch
   * panel's `renderOutput`, and for the same reason.
   *
   * Rendered **inside the first queued row**, once (task-337). The endpoint explains
   * the winner, so the disclosure belongs to the winner's card rather than sitting
   * after the list, where three tasks make it read as a card about nothing.
   */
  renderWhyThisOne?: () => React.ReactNode;
  /**
   * The machine-wide capacity row (task-328), supplied by the page for the same reason
   * `renderWhyThisOne` is: it owns a query, and this component is pure presentation.
   */
  renderMachineCapacity?: () => React.ReactNode;
  /**
   * The Dispatch control for one queued task (task-337), supplied by the page.
   *
   * Same render-prop shape as the two above, and for the same reason: starting a run
   * needs a mutation, the dispatch gates and a resolved human identity, none of which a
   * component rendered straight from a response object in its tests can have. Omitted,
   * the panel is exactly what it was before -- a list of links.
   */
  renderQueueAction?: (task: TaskRead) => React.ReactNode;
  /**
   * A closing note for the next-up panel: why the machine cannot dispatch, if it cannot.
   *
   * One line at the foot of the panel rather than a refusal repeated beside every row,
   * because the gate is a property of the machine and the project, not of the task.
   */
  renderQueueGate?: () => React.ReactNode;
  /**
   * Whether this machine has nothing running. `null` while that is still being read.
   *
   * The next-up panel says a different sentence for each answer, and says none of them
   * until it knows -- a line that flips from "nothing is running" to "something is"
   * one poll after the page paints is worse than a line that arrives a moment late.
   */
  machineIdle?: boolean | null;
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

function TaskCard({ task, projectId }: { task: TaskRead; projectId: string }) {
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
          <DependencyState task={task} compact />
        </div>
      </div>
    </Link>
  );
}

function CreateTaskLink({ projectId }: { projectId: string }) {
  return (
    <Link
      to={projectPath(projectId, "/tasks/new")}
      className="touch-target rounded-md bg-blue-600 px-4 py-2 text-sm font-semibold text-white hover:bg-blue-500"
    >
      Create task
    </Link>
  );
}

/**
 * How many drafts the backlog panel lists before it stops and links to the rest.
 *
 * The panel used to print every one. On this project that was 31 rows of identical
 * `spec`, under a sentence saying that nothing is blocked by any of them -- a filing
 * cabinet where the page's one call to action belongs (task-337). It is now a sample
 * with a count and a link, which is what the reader does with it anyway.
 */
const BACKLOG_PREVIEW = 5;

/**
 * What the next-up panel says about the machine, above the tasks it is offering.
 *
 * Three answers, and the third is silence. "Nothing is running" is the sentence that
 * makes the panel a call to action rather than a listing, so it must not be printed
 * while the runs query is still in flight and might be about to contradict it.
 */
function machineSentence(machineIdle: boolean | null | undefined): string | null {
  if (machineIdle === true) return "Nothing is running on this machine. Starting one of these is the useful move.";
  if (machineIdle === false) return "An agent is already working. These are next in line.";
  return null;
}

function NextAction({
  dashboard,
  projectId,
  renderWhyThisOne,
  renderQueueAction,
  renderQueueGate,
  machineIdle,
}: DashboardProps) {
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
              {dashboard.backlog_tasks.slice(0, BACKLOG_PREVIEW).map((task) => (
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
          {dashboard.backlog_tasks.length > BACKLOG_PREVIEW && (
            <Link to={`${base}/tasks?status=draft`} className="touch-target mt-2 block text-center text-sm text-blue-400 hover:text-blue-300">
              View all {dashboard.backlog_tasks.length} drafts →
            </Link>
          )}
        </section>
      );
    case "next_up": {
      // `queue_preview` is the head of the queue and `next_task` is its first element,
      // so the fallback is for one case only: a client reading a server that predates
      // task-337. Preferring the list over the single task everywhere else means the
      // panel and the why-this-one disclosure beside it cannot name different tasks.
      const preview = dashboard.queue_preview?.length
        ? dashboard.queue_preview
        : dashboard.next_task
          ? [dashboard.next_task]
          : [];
      if (preview.length === 0) return null;
      const sentence = machineSentence(machineIdle);
      return (
        <section data-testid="next-action" className="rounded-lg border border-dark-border bg-dark-surface p-6">
          <div className="mb-1 flex items-baseline justify-between gap-4">
            <h2 className="text-sm font-medium text-dark-text">Next up</h2>
            <Link to={`${base}/tasks?status=ready`} className="touch-target text-xs text-blue-400 hover:text-blue-300">All ready tasks →</Link>
          </div>
          {sentence && <p className="mb-3 text-xs text-dark-muted">{sentence}</p>}
          <ul className="space-y-2">
            {preview.map((task, index) => (
              <li
                key={task.id}
                data-testid="queue-preview-task"
                data-task-id={task.id}
                className="rounded-lg border border-dark-border bg-dark-bg"
              >
                <div className="flex flex-col gap-3 p-4 min-[820px]:flex-row min-[820px]:items-start min-[820px]:justify-between">
                  {/*
                    The row's action is a button, and a button inside an anchor is not
                    valid HTML -- so the link wraps the text and the control sits beside
                    it, rather than the whole row being one link as it was when the panel
                    could only ever link.
                  */}
                  <Link to={`${base}/tasks/${encodeURIComponent(task.id)}`} className="min-w-0 flex-1 hover:text-blue-300">
                    <div className="font-mono text-xs text-blue-400">{task.id}</div>
                    <h3 className="font-medium text-dark-text">{task.title}</h3>
                    <p className="mt-1 text-sm text-dark-muted">{truncate(task.spec.summary, 160)}</p>
                  </Link>
                  <div className="flex flex-shrink-0 flex-wrap items-center gap-2">
                    <Badge className="bg-dark-surface text-dark-muted">{task.priority}</Badge>
                    {renderQueueAction?.(task)}
                  </div>
                </div>
                {/*
                  The disclosure belongs to the *first* row, inside its card and under a
                  hairline, because what it explains is why that task is first. While the
                  panel offered one task it could sit at the foot and still be read that
                  way; offering three, a box after the list reads as a fourth card about
                  nothing in particular, which is what it looked like (Jeff, 2026-09-05).
                  It is deliberately not repeated per row: the endpoint explains the
                  winner, and there is no answer to give for the second.
                */}
                {index === 0 && renderWhyThisOne && (
                  <div className="border-t border-dark-border px-4 py-2">{renderWhyThisOne()}</div>
                )}
              </li>
            ))}
          </ul>
          {renderQueueGate?.()}
        </section>
      );
    }
    case "queue_broken":
      return (
        <section data-testid="next-action" className="rounded-lg border border-dark-border bg-dark-surface p-6">
          <h2 className="mb-1 text-sm font-medium text-dark-text">
            The queue cannot say what is next
          </h2>
          <p className="text-xs text-dark-muted">
            There is open work, and it may well be claimable — but two tasks claim one place
            in line, so nothing here can honestly tell you which comes first. The banner above
            names them and the command that repairs it.
          </p>
        </section>
      );
    case "nothing_claimable":
      return (
        <section data-testid="next-action" className="rounded-lg border border-dark-border bg-dark-surface p-6">
          <h2 className="mb-1 text-sm font-medium text-dark-text">Nothing claimable right now</h2>
          <p className="mb-4 text-xs text-dark-muted">
            Every open task is waiting on a dependency, or is an umbrella finished by its children. Adding work is the useful move.
          </p>
          <CreateTaskLink projectId={projectId} />
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
                  <div className="mt-1"><CreateTaskLink projectId={projectId} /></div>
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

export function Dashboard({
  dashboard,
  projectId,
  renderWhyThisOne,
  renderMachineCapacity,
  renderQueueAction,
  renderQueueGate,
  machineIdle,
}: DashboardProps) {
  const statTiles = [
    ["Needs you", dashboard.stats.waiting_for_human, "text-orange-400"],
    ["In Progress", dashboard.stats.in_progress, ""],
    ["Blocked", dashboard.stats.blocked, "text-red-400"],
    // "Completed", not "Done": the backend counts outcome == completed only, so a
    // superseded or cancelled task is closed but is not in this number. Calling it
    // Done invited the reader to subtract it from Total and find tasks missing.
    ["Completed", dashboard.stats.completed, "text-green-400"],
    ["Total", dashboard.stats.total, ""],
  ] as const;

  return (
    <div className="space-y-6">
      <BrokenFiles files={dashboard.broken_files} />
      {dashboard.queue_broken && (
        <QueueBroken
          problems={dashboard.queue_broken.problems ?? []}
          repairCommand={dashboard.queue_broken.repair_command}
        />
      )}
      <NextAction
        dashboard={dashboard}
        projectId={projectId}
        renderWhyThisOne={renderWhyThisOne}
        renderQueueAction={renderQueueAction}
        renderQueueGate={renderQueueGate}
        machineIdle={machineIdle}
      />
      <section className="overflow-hidden rounded-lg border border-dark-border bg-dark-surface" aria-label="Task statistics">
        <dl className="grid grid-cols-5 divide-x divide-dark-border">
          {statTiles.map(([label, count, className]) => (
            <div key={label} className="min-w-0 px-1 py-2 text-center min-[820px]:px-4 min-[820px]:py-3">
              <dt className="truncate text-[10px] font-medium uppercase tracking-wide text-dark-muted min-[820px]:text-xs">{label}</dt>
              <dd className={`mt-0.5 text-xl font-bold leading-none min-[820px]:text-2xl ${className}`}>{count}</dd>
            </div>
          ))}
        </dl>
        {dashboard.stats.awaiting_input > 0 && (
          <Link
            to={projectPath(projectId, "/tasks?status=draft")}
            className="touch-target flex w-full justify-center border-t border-dark-border px-3 text-xs text-dark-muted hover:bg-dark-border hover:text-blue-300"
          >
            +{dashboard.stats.awaiting_input} in backlog
          </Link>
        )}
        {/*
          A row at the foot of this card rather than a section of its own, and that is
          what pays for it. task-294 requires this page to fit one viewport, so a sixth
          top-level section would cost its own `space-y-6` gap, border and padding --
          about 110px -- before rendering a character. Here it costs one line, measured
          at 41px on a 390x844 phone, and it sits beside the backlog link, which is
          already exactly this shape. Nothing was removed to make room.
        */}
        {renderMachineCapacity?.()}
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
