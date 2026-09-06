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
   * The slot board (task-092), supplied by the page rather than built here.
   *
   * It owns the machine-wide runs query, and this component is otherwise pure
   * presentation rendered straight from a response object in its tests. Same shape as
   * the dispatch panel's `renderOutput`, and for the same reason.
   *
   * Called with `statusOnly`, which is how task-081's rule survives a grid of equal
   * cards. The board is not a rung of the ladder -- it is the machine's shape and it
   * shows on calm days and alarming ones alike -- but on an alarming day it is asked
   * for its occupied cells only. An occupied cell is a status readout; a free cell with
   * a Dispatch button is a nudge, and an alarm must never have to compete with one.
   *
   * Everything the next-up rung used to carry moved into it: the queue, the Dispatch
   * buttons, the why-this-one disclosure and the closed-gate line. The page wires those
   * straight to the board, so they are no longer this component's business.
   */
  renderSlotBoard?: (statusOnly: boolean) => React.ReactNode;
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

/**
 * How many active tasks the Dashboard lists before it stops and links to the rest.
 *
 * `active_tasks` is uncapped by the server -- it is every open task in the project --
 * and it used to be rendered in full. On this project that was forty cards, four
 * thousand pixels of them, under a board that had already answered the question the
 * page exists to answer (task-294). The count in the heading and the link beside it
 * say what the four rows are a sample of.
 */
/**
 * The floor under the tail region, so it cannot be squeezed out of existence.
 *
 * The glance takes the space it needs first, and on a phone at six slots it needs more
 * than there is. Without a floor the tail would resolve to zero pixels and the two
 * sections in it would be unreachable rather than merely short -- which is the one
 * outcome task-294 rules out. Six rem is a section heading and the top of its first
 * row: enough to see that there is something there and to scroll it.
 */
const TAIL_MIN = "min-h-[6rem]";

const ACTIVE_PREVIEW = 3;

/**
 * How many of the ten log entries the server sends the Dashboard prints.
 *
 * Four, one line each. The feed is the least glance-like thing on the page and the
 * furthest from a decision; nothing in it is a link, so a shorter list makes nothing
 * unreachable -- every task it names is on the board, in the list above it, or a search
 * away on the Tasks surface.
 */
const UPDATES_PREVIEW = 3;

/**
 * One active task, at the density a bounded frame can afford (task-294).
 *
 * `text-lg` over a full-width summary and `p-4` around it made each of these 100px
 * tall, which is a seventh of a phone screen for one row of a list that is a sample.
 * Everything a reader picks a row by is still here -- title, summary, priority and
 * whether it is blocked -- on one line each.
 */
function TaskCard({ task, projectId }: { task: TaskRead; projectId: string }) {
  return (
    <Link
      to={projectPath(projectId, `/tasks/${encodeURIComponent(task.id)}`)}
      className="touch-target block overflow-hidden rounded-lg border border-dark-border bg-dark-surface px-3 py-2 transition hover:border-blue-500"
    >
      <div className="flex flex-col items-start justify-between gap-1 min-[820px]:flex-row min-[820px]:items-center min-[820px]:gap-4">
        <div className="min-w-0">
          <h3 className="truncate text-sm font-medium">{task.title}</h3>
          <p className="truncate text-xs text-dark-muted">{truncate(task.spec.summary, 120)}</p>
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
 * The ladder, minus the rung the board absorbed.
 *
 * `next_up` used to live here: the head of the claimable queue with a Dispatch button
 * on each row. It is now the free half of the slot board, one cell per free run slot,
 * which is the same offer arranged by the machine's capacity instead of by a list
 * length. Every other rung is unchanged, including the strictness task-081 gave them.
 */

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
          {/* Task, Title, Reason. The id and the reason are short and bounded; the
              title is neither, so under a fixed layout it is the column that should
              absorb whatever width is going. */}
          <ResponsiveTable aria-label="Backlog awaiting your input" columns={["9rem", null, "8rem"]}>
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
    case "next_up":
      // Absorbed by the slot board (task-092). The rung stays in the endpoint's
      // vocabulary -- it is still the honest name for "there is claimable work" and
      // `Dashboard` reads it to decide where the board goes -- but the panel it used to
      // draw is now one free cell per free run slot, which is the same offer arranged by
      // the machine's capacity rather than by a list length.
      return null;
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

export function Dashboard({ dashboard, projectId, renderSlotBoard }: DashboardProps) {
  /**
   * Whether an alarm holds the page.
   *
   * These are the two rungs task-081 made strict, and they are the two the board must
   * not stand beside as an equal card: "work has stopped on these until you act" and
   * "the queue cannot say what is next" are both alarms, and a grid of six confident
   * cells under either would be exactly the competition that task fixed. The board
   * still renders -- what is running is worth knowing on a bad day too -- but below the
   * alarm and with its free cells withheld, so the page keeps one call to action.
   */
  const alarming = dashboard.next_action === "blocked" || dashboard.next_action === "queue_broken";
  const conditions = dashboard.broken_files.length > 0 || Boolean(dashboard.queue_broken);

  /**
   * The Dashboard is a frame of exactly one viewport, and this is its inside (task-294).
   *
   * Three regions, and which one gets the space when there is not enough is the whole
   * design:
   *
   * - **Conditions** -- an unreadable task file, a duplicated queue position. Pinned,
   *   because these are states of the system rather than content, and a state that can
   *   be scrolled out of the frame is one nobody acts on.
   * - **The glance** -- the slot board and the one call to action. This is what the
   *   page is *for* (task-092, task-081), so it is sized to its content and takes the
   *   space it needs before anything else gets any.
   * - **The tail** -- active tasks and recent updates. Whatever is left, with its own
   *   scroll, never less than `TAIL_MIN` so it cannot vanish and take its content with
   *   it. Both sections are samples that link to the surface holding the whole.
   *
   * `min-h-0` on the column and on the glance is the load-bearing half: a flex child's
   * default `min-height: auto` refuses to shrink below its content, so without it a
   * six-slot board on a phone would push straight through the frame and be clipped by
   * the shell's `overflow-hidden` instead of scrolling inside its own region.
   *
   * Measured at 390x844 with a forty-task backlog: the board is 610px of the 747px
   * inside the frame at three slots, which is why the tail is a remainder rather than a
   * list that was going to fit if only it were shorter.
   */
  return (
    <div data-testid="dashboard" className="flex min-h-0 flex-1 flex-col gap-3">
      {conditions && (
        <div className="shrink-0 space-y-4">
          <BrokenFiles files={dashboard.broken_files} />
          {dashboard.queue_broken && (
            <QueueBroken
              problems={dashboard.queue_broken.problems ?? []}
              repairCommand={dashboard.queue_broken.repair_command}
            />
          )}
        </div>
      )}
      {/*
        Order, not just presence. On a calm day the board is the answer to "what do I do
        next" and goes first; on an alarming one the alarm does, and the board follows
        it as a status readout. `empty_project` gets no board at all -- a new project has
        no runs and no queue, so six empty cells above "Getting Started" would be the
        page's loudest element saying nothing.
      */}
      <div
        data-testid="dashboard-glance"
        className="flex min-h-0 flex-col gap-3 overflow-y-auto"
      >
        {alarming ? (
          <>
            <NextAction dashboard={dashboard} projectId={projectId} />
            {renderSlotBoard?.(true)}
          </>
        ) : (
          <>
            {dashboard.next_action !== "empty_project" && renderSlotBoard?.(false)}
            <NextAction dashboard={dashboard} projectId={projectId} />
          </>
        )}
      </div>
      <div
        data-testid="dashboard-tail"
        className={`flex ${TAIL_MIN} flex-1 flex-col gap-3 overflow-y-auto`}
      >
        <section className="shrink-0 rounded-lg border border-dark-border bg-dark-surface">
          <div className="flex items-baseline justify-between gap-4 border-b border-dark-border px-4 py-2">
            <h2 className="text-sm font-medium text-dark-text">
              Active tasks{" "}
              <span className="font-normal text-dark-muted">({dashboard.active_tasks.length})</span>
            </h2>
            <Link to={projectPath(projectId, "/tasks")} className="touch-target text-xs text-blue-400 hover:text-blue-300">
              {dashboard.active_tasks.length > ACTIVE_PREVIEW
                ? `View all ${dashboard.active_tasks.length} →`
                : "View all →"}
            </Link>
          </div>
          <div className="space-y-2 p-2">
            {dashboard.active_tasks.length > 0 ? dashboard.active_tasks.slice(0, ACTIVE_PREVIEW).map((task) => (
              <TaskCard key={task.id} task={task} projectId={projectId} />
            )) : (
              <div className="rounded-lg border border-dashed border-dark-border bg-dark-bg/40 p-4 text-center text-sm text-dark-muted">No active tasks right now. Enjoy the calm!</div>
            )}
          </div>
        </section>
        <section className="shrink-0 rounded-lg border border-dark-border bg-dark-surface">
          <div className="border-b border-dark-border px-4 py-2">
            <h2 className="text-sm font-medium text-dark-text">Recent updates</h2>
          </div>
          <div className="divide-y divide-dark-border">
            {dashboard.recent_updates.length > 0 ? dashboard.recent_updates.slice(0, UPDATES_PREVIEW).map((update, index) => (
              <div className="px-4 py-2" key={`${update.task_id}-${update.timestamp}-${index}`}>
                <div className="flex flex-wrap items-baseline gap-x-2 text-xs text-dark-muted">
                  <span className="font-medium text-dark-text">{update.task_title}</span>
                  <time dateTime={update.timestamp}>{new Date(update.timestamp).toLocaleString([], { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" })}</time>
                  <span>{update.author}</span>
                </div>
                <p className="truncate text-xs text-dark-muted">{update.summary}</p>
              </div>
            )) : <div className="px-4 py-3 text-sm text-dark-muted">No recent updates. Check back soon.</div>}
          </div>
        </section>
      </div>
    </div>
  );
}
