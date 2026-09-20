import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Link,
  Navigate,
  Route,
  Routes,
  useLocation,
  useMatch,
  useNavigate,
  useOutlet,
  useParams,
} from "react-router-dom";

import {
  appendLogEntryApiProjectsProjectIdTasksTaskIdLogPostMutation,
  cancelDispatchRunApiProjectsProjectIdDispatchRunsRunIdCancelPostMutation,
  armPullModeApiProjectsProjectIdDispatchArmPostMutation,
  disableDispatchApiProjectsProjectIdDispatchDisablePostMutation,
  disarmPullModeApiProjectsProjectIdDispatchDisarmPostMutation,
  enableDispatchApiProjectsProjectIdDispatchEnablePostMutation,
  getAnalyticsApiProjectsProjectIdAnalyticsGetOptions,
  getDashboardApiProjectsProjectIdDashboardGetOptions,
  getDispatchStateApiProjectsProjectIdDispatchGetOptions,
  getPlaybooksApiProjectsProjectIdPlaybooksGetOptions,
  getProjectsApiProjectsGetOptions,
  getQueueApiProjectsProjectIdQueueGetOptions,
  getTaskDetailApiProjectsProjectIdTasksTaskIdDetailGetOptions,
  approveTaskApiProjectsProjectIdTasksTaskIdApprovePostMutation,
  createTaskApiProjectsProjectIdTasksPostMutation,
  dispatchTaskEndpointApiProjectsProjectIdTasksTaskIdDispatchPostMutation,
  listBrokenTasksApiProjectsProjectIdTasksBrokenGetOptions,
  listDispatchRunsApiProjectsProjectIdDispatchRunsGetOptions,
  listTasksApiProjectsProjectIdTasksGetOptions,
  promoteTaskApiProjectsProjectIdTasksTaskIdPromotePostMutation,
  queueKeepTaskApiProjectsProjectIdTasksTaskIdQueueKeepPostMutation,
  queueMoveTaskApiProjectsProjectIdTasksTaskIdQueueMovePostMutation,
  readTaskFinishApiProjectsProjectIdDispatchFinishesTaskIdGetOptions,
  rejectTaskApiProjectsProjectIdTasksTaskIdRejectPostMutation,
  reprioritizeTaskApiProjectsProjectIdTasksTaskIdReprioritizePostMutation,
  answerTaskApiProjectsProjectIdTasksTaskIdAnswerPostMutation,
  holdTaskApiProjectsProjectIdTasksTaskIdHoldPostMutation,
  redirectTaskApiProjectsProjectIdTasksTaskIdRedirectPostMutation,
  requestChangesApiProjectsProjectIdTasksTaskIdRequestChangesPostMutation,
  resumeTaskApiProjectsProjectIdTasksTaskIdResumePostMutation,
  runPlaybookEndpointApiProjectsProjectIdPlaybooksNameRunPostMutation,
  updateTaskApiProjectsProjectIdTasksTaskIdPatchMutation,
} from "./api/generated/@tanstack/react-query.gen";
import type {
  AttentionResponse,
  DispatchRunView,
  MutationResultOutput,
  Priority,
  QueuedDispatchView,
} from "./api/types";
import { readRefusal } from "./api/mutation-error";
import { readReportContext } from "./report/issueReport";
import {
  requireSupportedTaskSchemas,
  UnsupportedTaskSchemaError,
} from "./api/schema-version";
import { Analytics } from "./components/Analytics";
import { DEFAULT_ANALYTICS_RANGE } from "./components/analyticsSecondSet";
import { BrokenFiles } from "./components/BrokenFiles";
import { Dashboard } from "./components/Dashboard";
import { ConnectionUnavailable } from "./components/ConnectionUnavailable";
import {
  DispatchSettings,
  runsPollInterval,
  type DispatchOptions,
  type DispatchRefusal,
} from "./components/DispatchPanel";
import { DispatchRunOutput } from "./components/DispatchOutput";
import { finishPollInterval } from "./components/FinishPanel";
import { TaskList, type ReorderHandlers, type TaskListVariant } from "./components/TaskList";
import { TaskDetail } from "./components/TaskDetail";
import { CaptureForm } from "./components/CaptureForm";
import { GlobalCapture } from "./components/CaptureControl";
import { NextExplanation } from "./components/NextExplanation";
import { invalidateProjectTaskQueries, LiveUpdateStatus } from "./components/LiveUpdates";
import { LiveRunCount, LiveRunsPage, useLiveRuns } from "./components/LiveRuns";
import { RecentlyFinished, useRecentClosures } from "./components/RecentlyFinished";
import { IdleSessionsSection } from "./components/IdleSessions";
import { Playbooks, type PlaybookRunRequest } from "./components/Playbooks";
import { AttentionBadge, useAttention } from "./components/AttentionBadge";
import {
  AttentionNotifier,
  NotificationDelivery,
  useAcknowledgeAttention,
  useAcknowledgeFromUrl,
  useAcknowledgeOnOpen,
} from "./components/attention/WindowsAttention";
import { MobilePush } from "./components/attention/MobilePush";
import { PrimaryNav } from "./components/PrimaryNav";
import { QueueBroken } from "./components/QueueBroken";
import { QueueDispatch, QueueDispatchGate } from "./components/QueueDispatch";
import { useWideShell } from "./components/shellLayout";
import { SlotBoard } from "./components/SlotBoard";
import { VersionSkew } from "./components/VersionSkew";

function ProjectRedirect() {
  const navigate = useNavigate();
  const projectsQuery = useQuery(getProjectsApiProjectsGetOptions());

  useEffect(() => {
    const firstProject = projectsQuery.data?.[0];
    if (firstProject) {
      navigate(`/p/${encodeURIComponent(firstProject.id)}`, { replace: true });
    }
  }, [navigate, projectsQuery.data]);

  if (projectsQuery.isPending) {
    return <StatusCard title="Opening AgentJobs...">Resolving the first registered project.</StatusCard>;
  }

  if (projectsQuery.isError) {
    return (
      <StatusCard title="AgentJobs could not load the project registry">
        <p>Confirm the local server is running, then reload this page.</p>
      </StatusCard>
    );
  }

  return (
    <StatusCard title="No projects are registered">
      <p>Register or create a project before opening the React app.</p>
      <a className="mt-4 inline-block font-semibold text-blue-300 hover:text-blue-200" href="/projects/new">
        Add or create a project
      </a>
    </StatusCard>
  );
}

function DashboardPage({ projectId }: { projectId: string }) {
  const dispatch = useDashboardDispatch(projectId);
  // Null until the machine-wide answer arrives, and the board draws nothing until then:
  // it is the ceiling in this body that decides how many cells there are, and a board
  // that guessed a shape and corrected it one poll later would be worse than one that
  // arrives a moment late.
  const liveRuns = useLiveRuns();
  // Machine-wide like the board's, and null until it answers -- the region draws its
  // heading and waits rather than claiming nothing has finished (task-460).
  const closures = useRecentClosures();
  const dashboardQuery = useQuery({
    ...getDashboardApiProjectsProjectIdDashboardGetOptions({
      path: { project_id: projectId },
    }),
    select: (dashboard) => {
      requireSupportedTaskSchemas([
        ...dashboard.active_tasks,
        ...dashboard.waiting_tasks,
        ...dashboard.backlog_tasks,
        ...(dashboard.next_task ? [dashboard.next_task] : []),
      ]);
      return dashboard;
    },
  });

  if (dashboardQuery.error instanceof UnsupportedTaskSchemaError) {
    return (
      <StatusCard title="Unsupported task schema">
        <p>{dashboardQuery.error.message}</p>
        <p className="mt-4">Upgrade the UI before viewing this project.</p>
      </StatusCard>
    );
  }

  if (dashboardQuery.isPending) {
    return <StatusCard title="Opening dashboard...">Loading current project data.</StatusCard>;
  }

  if (dashboardQuery.isError && !dashboardQuery.data) {
    return <ConnectionUnavailable offline={false} />;
  }

  const identity = dashboardQuery.data.identity;

  // `queue_preview` is the claimable frontier and `next_task` is its first element, so
  // the fallback is for one case only: a client reading a server that predates
  // task-337. Preferring the list everywhere else means the board and the why-this-one
  // disclosure inside its first free cell cannot name different tasks.
  const queue = dashboardQuery.data.queue_preview?.length
    ? dashboardQuery.data.queue_preview
    : dashboardQuery.data.next_task
      ? [dashboardQuery.data.next_task]
      : [];

  return (
    <Dashboard
      dashboard={dashboardQuery.data}
      projectId={projectId}
      renderSlotBoard={(statusOnly) => (
        <SlotBoard
          body={liveRuns}
          queue={queue}
          projectId={projectId}
          statusOnly={statusOnly}
          renderWhyThisOne={() => <NextExplanation projectId={projectId} />}
          renderQueueAction={(task) => (
            <QueueDispatch
              state={dispatch.state}
              user={identity.ok ? identity.user : null}
              identityDetail={identity.detail}
              // Server-computed, by the same function the dispatch gate calls. This
              // used to be `Boolean(task.spec.description?.trim())` here, with a comment
              // admitting that drift between the two expressions would cost a link
              // instead of a button; the card carries the answer instead of the field
              // (task-495).
              canBrief={task.can_brief}
              taskHref={`/p/${encodeURIComponent(projectId)}/tasks/${encodeURIComponent(task.id)}`}
              busy={dispatch.startingTaskId === task.id}
              refusal={dispatch.refusal?.taskId === task.id ? dispatch.refusal.refusal : null}
              onDispatch={() => void dispatch.start(task.id, identity.ok ? identity.user : null)}
            />
          )}
          renderQueueGate={() => (
            <QueueDispatchGate state={dispatch.state} projectId={projectId} />
          )}
          renderQueuedAction={(entry) => (
            <button
              type="button"
              data-testid="cancel-queued-dispatch"
              disabled={dispatch.cancellingQueueId === entry.queue_id}
              onClick={() => void dispatch.cancelQueued(entry)}
              className="touch-target rounded-lg border border-dark-border px-2 text-xs text-dark-muted hover:border-orange-700/70 hover:text-orange-200 disabled:opacity-60"
            >
              {dispatch.cancellingQueueId === entry.queue_id ? "Cancelling…" : "Cancel"}
            </button>
          )}
          renderArmedAction={(entry) => (
            <button
              type="button"
              data-testid="disarm-pull-mode"
              disabled={dispatch.disarmingProject === entry.project_id}
              onClick={() => void dispatch.disarm(entry.project_id)}
              className="touch-target rounded-lg border border-dark-border px-2 text-xs text-dark-muted hover:border-red-700/70 hover:text-red-200 disabled:opacity-60"
            >
              {dispatch.disarmingProject === entry.project_id ? "Disarming…" : "Disarm"}
            </button>
          )}
        />
      )}
      renderRecentlyFinished={() => <RecentlyFinished body={closures} projectId={projectId} />}
      renderNotificationDelivery={() => (
        <>
          <NotificationDelivery />
          {/* Beside the desktop notice, not on a page of its own: they are two answers
              to one question -- how does AgentJobs reach me when I am not looking at
              this -- and separating them would make the phone half the one nobody
              finds (task-423). */}
          <MobilePush projectId={projectId} />
        </>
      )}
    />
  );
}

/**
 * Starting a run from the Dashboard's next-up panel (task-337).
 *
 * Deliberately thinner than {@link useTaskDispatch}: no runs list and no poller. This
 * panel offers a button and then gets out of the way -- what the run does next is the
 * task page's subject, and the machine-wide capacity row below already says that
 * something is running. Polling a per-task runs endpoint for each of three tasks would
 * be three requests every two seconds to tell the reader something one request already
 * tells them.
 *
 * The in-flight task id and the refusal are held per task rather than as one flag,
 * because three buttons share this hook: a single `busy` would grey out all three, and a
 * single refusal would print the first task's failure beside the third one's button.
 */
function useDashboardDispatch(projectId: string) {
  const queryClient = useQueryClient();
  const [startingTaskId, setStartingTaskId] = useState<string | null>(null);
  const [cancellingQueueId, setCancellingQueueId] = useState<string | null>(null);
  const [disarmingProject, setDisarmingProject] = useState<string | null>(null);
  const [refusal, setRefusal] = useState<{ taskId: string; refusal: DispatchRefusal } | null>(null);

  // The same endpoint the task page, the playbooks page and the settings page read, so
  // no two surfaces can disagree about whether this machine may dispatch.
  const stateQuery = useQuery(
    getDispatchStateApiProjectsProjectIdDispatchGetOptions({ path: { project_id: projectId } }),
  );
  const start = useMutation(
    dispatchTaskEndpointApiProjectsProjectIdTasksTaskIdDispatchPostMutation(),
  );
  // The same route that cancels a run, because a waiting dispatch and the run it
  // becomes are one card to whoever is looking at it (task-459). The server tries the
  // queue first and falls through to the run, so a click that lands a moment late stops
  // the agent rather than reporting a cancellation that did not happen.
  const cancelQueued = useMutation(
    cancelDispatchRunApiProjectsProjectIdDispatchRunsRunIdCancelPostMutation(),
  );
  // Disarming from the board rather than only from the settings page (task-462),
  // because the board is where a person is when they decide they have seen enough. It
  // kills nothing, so it is safe to offer next to the thing it stops.
  const disarm = useMutation(disarmPullModeApiProjectsProjectIdDispatchDisarmPostMutation());

  return {
    state: stateQuery.data ?? null,
    startingTaskId,
    cancellingQueueId,
    disarmingProject,
    refusal,
    disarm: async (armedProjectId: string): Promise<void> => {
      setDisarmingProject(armedProjectId);
      try {
        // The armed project's own id, never the page's: the rail is machine-wide.
        await disarm.mutateAsync({ path: { project_id: armedProjectId || projectId } });
      } finally {
        setDisarmingProject(null);
        await queryClient.invalidateQueries();
      }
    },
    cancelQueued: async (entry: QueuedDispatchView): Promise<void> => {
      setCancellingQueueId(entry.queue_id);
      try {
        await cancelQueued.mutateAsync({
          // The entry's own project, never the page's: the rail is machine-wide and
          // most of what is on it belongs to somebody else's project.
          path: { project_id: entry.project_id || projectId, run_id: entry.queue_id },
        });
      } finally {
        setCancellingQueueId(null);
        await queryClient.invalidateQueries();
      }
    },
    start: async (taskId: string, user: string | null): Promise<void> => {
      setRefusal(null);
      setStartingTaskId(taskId);
      try {
        // `user` names who is clicking and the server writes their authorising entry
        // before it starts anything -- the same one-click contract task-188 established
        // for the task page. Nothing else is sent: a group, a posture or a brief is a
        // choice, and choosing happens on the task's own page.
        await start.mutateAsync({
          path: { project_id: projectId, task_id: taskId },
          body: { ...(user ? { user } : {}) },
        });
      } catch (error) {
        const read = readRefusal(error);
        setRefusal({
          taskId,
          refusal: read
            ? { reason: read.code, message: read.message, suggestedAction: read.suggestedAction }
            : {
                reason: "unreachable",
                message: "AgentJobs could not be reached to start a run.",
              },
        });
      } finally {
        setStartingTaskId(null);
        // A dispatch claims the task, so the queue this panel is showing is stale the
        // moment it returns -- including on a refusal, which may be a refusal precisely
        // because something else claimed it first.
        await queryClient.invalidateQueries();
      }
    },
  };
}

function LiveRunsRoute() {
  return (
    <div className="space-y-6">
      <LiveRunsPage body={useLiveRuns()} />
      <IdleSessionsSection />
    </div>
  );
}

function TaskListPage({
  projectId,
  variant = "table",
}: {
  projectId: string;
  variant?: TaskListVariant;
}) {
  const queryClient = useQueryClient();
  const tasksQuery = useQuery({
    ...listTasksApiProjectsProjectIdTasksGetOptions({ path: { project_id: projectId } }),
    select: (tasks) => {
      requireSupportedTaskSchemas(tasks);
      return tasks;
    },
  });
  // Read for `problems` alone -- the order itself comes with the tasks, and the banner
  // that names the problems belongs to the surface rather than to this list (task-237).
  // What is left here is the one thing the list itself has to know: which bands it must
  // stop offering to reorder.
  const queueQuery = useQuery(
    getQueueApiProjectsProjectIdQueueGetOptions({ path: { project_id: projectId } }),
  );
  const projectsQuery = useQuery(getProjectsApiProjectsGetOptions());
  const actor = projectsQuery.data?.find((entry) => entry.id === projectId)?.default_user ?? null;
  const move = useMutation(queueMoveTaskApiProjectsProjectIdTasksTaskIdQueueMovePostMutation());
  const keep = useMutation(queueKeepTaskApiProjectsProjectIdTasksTaskIdQueueKeepPostMutation());
  const reprioritize = useMutation(
    reprioritizeTaskApiProjectsProjectIdTasksTaskIdReprioritizePostMutation(),
  );

  if (tasksQuery.error instanceof UnsupportedTaskSchemaError) {
    return <StatusCard title="Unsupported task schema"><p>{tasksQuery.error.message}</p><p className="mt-4">Upgrade the UI before viewing this project.</p></StatusCard>;
  }
  if (tasksQuery.isPending) return <StatusCard title="Opening tasks...">Loading current task data.</StatusCard>;
  if (!tasksQuery.data) return <ConnectionUnavailable offline={false} />;

  const tasks = tasksQuery.data;
  const revisionOf = (taskId: string) => tasks.find((task) => task.id === taskId)?.updated;
  // Every reorder is attributed and retry-safe: `actor` and `operation_id` are required
  // on both of these routes rather than optional, unlike the older verbs.
  const reorder: ReorderHandlers | null = actor
    ? {
        move: async (taskId, placement) => {
          // No expected_revision, deliberately -- the same call the note composer makes
          // and for the same reason. A move is not a decision taken against a snapshot:
          // it names a neighbour by id, and the manager resolves that under the queue
          // lock against whatever the band is at the time, so it does what was asked
          // however much the band moved in between. `top` and `bottom` are absolute and
          // need no snapshot at all.
          //
          // Sending one actively breaks the primary path. Alt+Up twice in quick
          // succession is one gesture as far as a person is concerned, and the second
          // keypress lands before the first move's refetch does -- so the revision on
          // screen is one write behind, the move is refused, and the reorder they just
          // watched happen rolls back. Nothing about that is a conflict worth reporting.
          //
          // `envelope=true` because the queue-move check's findings are a property of
          // the move, not of the task: a caller that read the task back afterwards
          // could not recover what the person who moved it was told. The bare-task
          // response is still the route's default for everybody else.
          const result = (await move.mutateAsync({
            path: { project_id: projectId, task_id: taskId },
            query: { envelope: true },
            body: { actor, operation_id: crypto.randomUUID(), ...placement },
          })) as MutationResultOutput;
          await invalidateProjectTaskQueries(queryClient, projectId);
          return {
            warnings: result.queue_warnings ?? [],
            undo: result.queue_undo ?? null,
          };
        },
        keep: async (taskId) => {
          // No expected_revision. The move this answers has already landed and the
          // refetch it triggered is what put the notice on screen, so the only writer
          // between the two is this browser -- and a refusal here would leave a person
          // who clicked Keep with no anchor and no explanation.
          await keep.mutateAsync({
            path: { project_id: projectId, task_id: taskId },
            body: { actor, operation_id: crypto.randomUUID() },
          });
          await invalidateProjectTaskQueries(queryClient, projectId);
        },
        reprioritize: async (taskId, priority, before) => {
          // This one keeps its revision. A band change is decided by a person reading a
          // confirmation panel that describes specific state, one gesture at a time, so
          // a task that moved since that panel was drawn should refuse rather than
          // reprioritise on the strength of a screen nobody has re-read.
          await reprioritize.mutateAsync({
            path: { project_id: projectId, task_id: taskId },
            body: {
              actor,
              operation_id: crypto.randomUUID(),
              expected_revision: revisionOf(taskId),
              priority: priority as Priority,
              before,
            },
          });
          await invalidateProjectTaskQueries(queryClient, projectId);
        },
      }
    : null;

  return (
    <TaskList
      tasks={tasks}
      projectId={projectId}
      variant={variant}
      queueProblems={queueQuery.data?.problems ?? []}
      reorder={reorder}
      reorderUnavailable={
        actor
          ? null
          : "Reordering is off because this request does not resolve to a person, and every queue move is recorded against one. Open a task to see the reason and the file to change: either this project configures no human actor, or it configures several and nothing said which of them you are."
      }
    />
  );
}

/**
 * The Tasks surface: a persistent list region and a detail region beside it.
 *
 * This is the shape task-235 decided and the only surface that has it. The Dashboard,
 * Create and Dispatch keep the full width, and the header nav remains the way you move
 * between them -- the region on the left is a *master column over tasks*, not a
 * navigation rail, so it belongs only where tasks are the subject.
 *
 * Three properties are load-bearing rather than stylistic:
 *
 *  - **`tasks` and `tasks/:taskId` are one route, not two.** They were siblings, which
 *    is precisely why opening a task unmounted the list, threw its scroll position away
 *    and made comparing two records a browser round trip. The child renders into the
 *    outlet here, so selecting a task changes only the right-hand region.
 *  - **Each region owns its scrolling and the page owns none.** Two regions that scroll
 *    the page as one unit is the failure mode that makes a sidebar layout feel wrong:
 *    reading to the bottom of a record would carry the list off the top of the screen.
 *    `ProjectApp` gives this surface the viewport's height for the same reason.
 *  - **The banners are outside both regions.** `BrokenFiles` and `QueueBroken` say the
 *    corpus is unreadable or the order is untrustworthy. Inside a scrollport either
 *    could be scrolled past, and neither would be visible at all from the other region.
 *
 * Below the device-class threshold this renders what it rendered before the two regions
 * existed: one thing at a time, the list until a task is chosen and the record after.
 * That is the phone default of task-235's rule -- the list is hidden, not absent -- and
 * task-238 adds the control that opens it at any width.
 */
function TasksSurface({ projectId }: { projectId: string }) {
  const outlet = useOutlet();
  const wide = useWideShell();
  // The same two queries the list used to make for these banners, so react-query serves
  // both callers from one cache entry and the surface costs no extra request.
  const brokenQuery = useQuery(
    listBrokenTasksApiProjectsProjectIdTasksBrokenGetOptions({ path: { project_id: projectId } }),
  );
  const queueQuery = useQuery(
    getQueueApiProjectsProjectIdQueueGetOptions({ path: { project_id: projectId } }),
  );
  const banners = (
    <>
      <BrokenFiles files={brokenQuery.data ?? []} />
      <QueueBroken
        problems={queueQuery.data?.problems ?? []}
        repairCommand={queueQuery.data?.repair_command ?? "agentjobs queue repair"}
      />
    </>
  );

  if (!wide) {
    return (
      <div className="space-y-6" data-shell="stacked">
        {banners}
        {outlet ?? <TaskListPage projectId={projectId} />}
      </div>
    );
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-4" data-shell="two-region">
      {banners}
      {/* A share of the width rather than a fixed column: 20rem is the floor a task row
          needs, and a third of a 2560px monitor is a readable list where a fixed 24rem
          would leave the same margin this epic exists to reclaim. Capped so the list
          stops growing at the point where it has stopped helping. */}
      <div className="grid min-h-0 flex-1 grid-cols-[minmax(20rem,min(34%,36rem))_minmax(0,1fr)] gap-6">
        <aside
          aria-label="Task list"
          data-region="list"
          className="min-h-0 overflow-y-auto pr-1"
        >
          <TaskListPage projectId={projectId} variant="tree" />
        </aside>
        <section aria-label="Task detail" data-region="detail" className="min-h-0 overflow-y-auto">
          {/* The region is the width the list leaves, and the reading measure is not
              here (task-239). It was, for one commit: a single `max-w-5xl` around the
              whole record, which fixed the 200-character line and took the same width
              back from the dependency graph, the log and the run transcript -- the
              parts of a record that are *better* wide. `TaskDetail` now caps its own
              prose and lets the wide blocks run, so this wrapper only has to be full
              width and let it. */}
          <div className="w-full">
            {outlet ?? <NoTaskSelected projectId={projectId} />}
          </div>
        </section>
      </div>
    </div>
  );
}

/**
 * What the detail region holds before anything is selected.
 *
 * Deliberately a prompt rather than a summary or the queue's next task. The one thing a
 * reader landing on `/tasks` does not know is that this region is now *theirs* -- that
 * picking a task fills it and leaves the list where it is -- and a panel of content
 * would answer a question nobody asked while hiding the one thing they need to be told
 * once. Rendering the next task here was the alternative and was rejected: the
 * Dashboard's next-up panel already answers it, two surfaces naming different tasks is
 * how they drift apart, and it would add a query to a region whose whole job is to be
 * replaced by the first click.
 */
function NoTaskSelected({ projectId }: { projectId: string }) {
  return (
    <section
      aria-label="No task selected"
      className="max-w-4xl rounded-2xl border border-dashed border-dark-border p-8"
    >
      <h2 className="text-lg font-semibold text-dark-text">No task selected</h2>
      <p className="mt-2 max-w-prose text-sm text-dark-muted">
        Choose a task in the list and its record opens here. The list stays where it is,
        so you can read one task against another without losing your place in the queue.
      </p>
      <Link
        to={projectPath(projectId, "/tasks/new")}
        className="mt-6 inline-block font-semibold text-blue-300 hover:text-blue-200"
      >
        File a new task
      </Link>
    </section>
  );
}

/**
 * Everything the dispatch panel needs for one task, in one hook.
 *
 * The runs query polls on its own clock rather than waiting for the revision poller,
 * because a run's progress is not a task write: the process is alive, the meta file is
 * changing, and the task YAML has not moved since the dispatch entry was written. A
 * page that only refetched on revision changes would show "Running for 3s" until the
 * run ended. It stops polling the moment nothing is live, so an idle task costs the
 * same as it did before dispatch existed.
 */
function useTaskDispatch(projectId: string, taskId: string, user: string | null) {
  const queryClient = useQueryClient();
  const [refusal, setRefusal] = useState<DispatchRefusal | null>(null);
  const [cancellingRunId, setCancellingRunId] = useState<string | null>(null);
  const [queuedNotice, setQueuedNotice] = useState<string | null>(null);

  const stateQuery = useQuery(
    getDispatchStateApiProjectsProjectIdDispatchGetOptions({ path: { project_id: projectId } }),
  );
  const runsQuery = useQuery({
    ...listDispatchRunsApiProjectsProjectIdDispatchRunsGetOptions({
      path: { project_id: projectId },
      query: { task_id: taskId },
    }),
    refetchInterval: (query) => runsPollInterval(query.state.data ?? []),
  });
  const start = useMutation(
    dispatchTaskEndpointApiProjectsProjectIdTasksTaskIdDispatchPostMutation(),
  );
  const cancel = useMutation(
    cancelDispatchRunApiProjectsProjectIdDispatchRunsRunIdCancelPostMutation(),
  );

  const runs = runsQuery.data ?? [];
  return {
    state: stateQuery.data ?? null,
    runs,
    // Each run brings its own output panel, which reads and polls for itself. The runs
    // list moves on the list's clock and a run's output on the poller's; tying them
    // together would mean either re-reading transcripts every two seconds or watching
    // an elapsed counter that updates five times slower than it should.
    renderOutput: (run: DispatchRunView) => <DispatchRunOutput key={run.run_id} run={run} />,
    busy: start.isPending,
    cancellingRunId,
    dispatchRefusal: refusal,
    onDispatch: async (options?: DispatchOptions): Promise<boolean> => {
      setRefusal(null);
      setQueuedNotice(null);
      let started = false;
      try {
        // `user` names who is clicking, and the server writes their authorising entry
        // before it starts anything — which is what makes this one click on a task an
        // agent filed. It is not the dispatch's actor and it is not its justification:
        // the entry is persisted first and the guard reads it back from storage.
        //
        // Null when no human is configured, in which case nothing is sent and the
        // server falls back to the pre-task-188 rule rather than signing the run with
        // whatever `default_user` happens to be. The panel disables the button before
        // it comes to that.
        const answer = await start.mutateAsync({
          path: { project_id: projectId, task_id: taskId },
          // `options` is spread rather than picked apart: its keys are absent unless
          // the human chose something, so a dispatch with nothing picked posts the
          // same body it posted before the group pulldown existed, and task-307's
          // posture arrives here without touching this call.
          body: { ...(user ? { user } : {}), ...(options ?? {}) },
        });
        // Both answers are 202 and only `queued` tells them apart (task-459). Reporting
        // a queued dispatch as started would tell somebody an agent is working when
        // nothing has started.
        if (answer?.queued) {
          setQueuedNotice(
            `Queued for the next free slot — place ${answer.queue_position || 1} in line. ` +
              "Nothing has started yet; every dispatch gate is checked when it does. " +
              "It is on the Dashboard's slot board, where it can be cancelled.",
          );
        }
        started = true;
      } catch (error) {
        const read = readRefusal(error);
        // Every guard has its own code and its own sentence. Collapsing them into
        // "dispatch failed" would leave a human retrying the one refusal that can
        // never succeed.
        setRefusal(
          read
            ? { reason: read.code, message: read.message, suggestedAction: read.suggestedAction }
            : {
                reason: "unreachable",
                message: "AgentJobs could not be reached to start a run.",
                suggestedAction: "Check that the server is still running, then try again.",
              },
        );
      }
      await queryClient.invalidateQueries();
      // Say whether a run started, rather than throwing. This handler is the thing that
      // turns a refusal into a sentence on screen, so it deliberately does not re-raise
      // — but the panel that asked for a brief needs the answer, because "did it start"
      // is what decides whether the human's text may be thrown away. Re-raising was the
      // alternative and it is worse here: the one-click caller invokes this as
      // `void onDispatch()`, so an exception would become an unhandled rejection that
      // every call site has to swallow to stay quiet, which is this same catch written
      // twice over.
      return started;
    },
    queuedNotice,
    onCancel: async (runId: string) => {
      setCancellingRunId(runId);
      try {
        await cancel.mutateAsync({ path: { project_id: projectId, run_id: runId } });
      } finally {
        setCancellingRunId(null);
        await queryClient.invalidateQueries();
      }
    },
  };
}

/**
 * What is happening to this task's branch, polled while it is happening (task-321).
 *
 * Its own query rather than a field on the task detail, because the two move on
 * completely different clocks: a task record changes when somebody writes to it, and a
 * finish changes every few seconds for three minutes. Folding this into the detail query
 * would mean either re-reading the whole task record every two seconds or watching a
 * finish that updates on the detail's clock, which is to say not at all.
 *
 * The page's own post-approve `invalidateQueries` is what starts this: `spawn_finish`
 * writes its marker before it spawns anything, so the refetch that follows an approval
 * cannot answer "nothing is happening" about a finish that request just started.
 */
function useTaskFinish(projectId: string, taskId: string) {
  const query = useQuery({
    ...readTaskFinishApiProjectsProjectIdDispatchFinishesTaskIdGetOptions({
      path: { project_id: projectId, task_id: taskId },
    }),
    refetchInterval: (state) => finishPollInterval(state.state.data ?? null),
  });
  return query.data ?? null;
}

function DispatchSettingsPage({ projectId }: { projectId: string }) {
  const queryClient = useQueryClient();
  const [error, setError] = useState<string | null>(null);
  const stateQuery = useQuery(
    getDispatchStateApiProjectsProjectIdDispatchGetOptions({ path: { project_id: projectId } }),
  );
  const enable = useMutation(enableDispatchApiProjectsProjectIdDispatchEnablePostMutation());
  const disable = useMutation(disableDispatchApiProjectsProjectIdDispatchDisablePostMutation());
  const armPull = useMutation(armPullModeApiProjectsProjectIdDispatchArmPostMutation());
  const disarmPull = useMutation(disarmPullModeApiProjectsProjectIdDispatchDisarmPostMutation());
  const after = async () => { await queryClient.invalidateQueries(); };

  return (
    <DispatchSettings
      state={stateQuery.data ?? null}
      busy={enable.isPending || disable.isPending || armPull.isPending || disarmPull.isPending}
      error={error}
      onEnable={async (target) => {
        setError(null);
        try {
          // The target is `{runner}` or `{group}`, never both -- the API refuses a body
          // naming each, and the control is one <select> so it cannot produce one.
          await enable.mutateAsync({ path: { project_id: projectId }, body: target });
        } catch (caught) {
          const refusal = readRefusal(caught);
          setError(refusal ? refusal.message : "Dispatch could not be enabled. Reload and try again.");
        }
        await after();
      }}
      onDisable={async () => {
        setError(null);
        try {
          await disable.mutateAsync({ path: { project_id: projectId } });
        } catch (caught) {
          const refusal = readRefusal(caught);
          setError(refusal ? refusal.message : "Dispatch could not be disabled. Reload and try again.");
        }
        await after();
      }}
      onArm={async (choice) => {
        setError(null);
        try {
          // `user` is left to the server, which resolves it from the principal this
          // request arrived on. Sending a name read out of a listing is exactly the
          // circular check task-332 removed, and arming is the last place to reintroduce
          // it: this name goes on every run the mode starts.
          await armPull.mutateAsync({ path: { project_id: projectId }, body: choice });
        } catch (caught) {
          const refusal = readRefusal(caught);
          setError(refusal ? refusal.message : "The pull mode could not be armed. Reload and try again.");
        }
        await after();
      }}
      onDisarm={async () => {
        setError(null);
        try {
          await disarmPull.mutateAsync({ path: { project_id: projectId } });
        } catch (caught) {
          const refusal = readRefusal(caught);
          setError(refusal ? refusal.message : "The pull mode could not be disarmed. Reload and try again.");
        }
        await after();
      }}
    />
  );
}

function TaskDetailPage({ projectId }: { projectId: string }) {
  const { taskId = "" } = useParams<{ taskId: string }>();
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const detailQuery = useQuery({
    ...getTaskDetailApiProjectsProjectIdTasksTaskIdDetailGetOptions({ path: { project_id: projectId, task_id: taskId } }),
    select: (detail) => {
      requireSupportedTaskSchemas([detail.task, ...detail.children, ...(detail.parent_task ? [detail.parent_task] : [])]);
      return detail;
    },
  });
  const approve = useMutation(approveTaskApiProjectsProjectIdTasksTaskIdApprovePostMutation());
  const changes = useMutation(requestChangesApiProjectsProjectIdTasksTaskIdRequestChangesPostMutation());
  const answer = useMutation(answerTaskApiProjectsProjectIdTasksTaskIdAnswerPostMutation());
  const redirect = useMutation(redirectTaskApiProjectsProjectIdTasksTaskIdRedirectPostMutation());
  const hold = useMutation(holdTaskApiProjectsProjectIdTasksTaskIdHoldPostMutation());
  const resume = useMutation(resumeTaskApiProjectsProjectIdTasksTaskIdResumePostMutation());
  const reject = useMutation(rejectTaskApiProjectsProjectIdTasksTaskIdRejectPostMutation());
  const promote = useMutation(promoteTaskApiProjectsProjectIdTasksTaskIdPromotePostMutation());
  const addNote = useMutation(appendLogEntryApiProjectsProjectIdTasksTaskIdLogPostMutation());
  const update = useMutation(updateTaskApiProjectsProjectIdTasksTaskIdPatchMutation());
  const [fieldsError, setFieldsError] = useState<string | null>(null);
  // The tag and category vocabulary the edit form completes against, fetched only once
  // somebody opens that form. It is the whole task list, which is the heaviest read
  // this API offers and worth nothing at all to the far larger number of people who
  // opened this page to read the record -- so it is not paid for on arrival. A lighter
  // route that answered "what words does this project already use" would be better
  // still, and is deliberately not part of this task: the constraint is that the UI
  // edits what the REST route already accepts, and inventing a read to make a datalist
  // cheaper is the kind of scope drift that turns a form into a sprint.
  const [wantVocabulary, setWantVocabulary] = useState(false);
  const vocabularyQuery = useQuery({
    ...listTasksApiProjectsProjectIdTasksGetOptions({ path: { project_id: projectId } }),
    enabled: wantVocabulary,
  });
  const vocabulary = useMemo(() => {
    const tags = new Set<string>();
    const categories = new Set<string>();
    for (const entry of vocabularyQuery.data ?? []) {
      for (const tag of entry.tags ?? []) tags.add(tag);
      if (entry.category) categories.add(entry.category);
    }
    return { tags: [...tags].sort(), categories: [...categories].sort() };
  }, [vocabularyQuery.data]);
  const [noteError, setNoteError] = useState<string | null>(null);
  // Held here rather than read off promote.error because a revision conflict is not
  // a failure to report and forget: the page reloads and the human is asked again,
  // so the explanation has to outlive the mutation that produced it.
  const [promoteError, setPromoteError] = useState<string | null>(null);
  const dispatch = useTaskDispatch(projectId, taskId, detailQuery.data?.identity.user ?? null);
  const finish = useTaskFinish(projectId, taskId);

  if (detailQuery.error instanceof UnsupportedTaskSchemaError) return <StatusCard title="Unsupported task schema">{detailQuery.error.message}</StatusCard>;
  if (detailQuery.isPending) return <StatusCard title="Opening task...">Loading the complete task record.</StatusCard>;
  if (detailQuery.isError && !detailQuery.data) return <StatusCard title="Task could not be loaded">Confirm the task still exists, then return to the list.</StatusCard>;
  const user = detailQuery.data.identity.user;
  const revision = detailQuery.data.task.updated;
  const refresh = async () => { await queryClient.invalidateQueries(); };
  // Every send-back reports through the one banner the panel already has: which
  // route failed is not a distinction a human can act on differently.
  const sendBacks = [changes, answer, redirect, hold, resume];
  const actionError =
    approve.error || reject.error || sendBacks.find((mutation) => mutation.error)?.error;
  return (
    <TaskDetail
      detail={detailQuery.data}
      projectId={projectId}
      busy={
        approve.isPending ||
        reject.isPending ||
        sendBacks.some((mutation) => mutation.isPending)
      }
      error={actionError ? "The action could not be recorded. Reload and try again." : null}
      promoteBusy={promote.isPending}
      promoteError={promoteError}
      // The waiting entry comes off the task record rather than the slot board, so the
      // panel's offer and the status chip in the page header are one answer (task-476).
      dispatch={{ ...dispatch, queuedDispatch: detailQuery.data.task.queued_dispatch ?? null }}
      finish={finish}
      onApprove={async (note) => { if (!user) return; await approve.mutateAsync({ path: { project_id: projectId, task_id: taskId }, body: { user, note } }); await refresh(); }}
      onResume={async (note) => { if (!user) return; await resume.mutateAsync({ path: { project_id: projectId, task_id: taskId }, body: { user, note } }); await refresh(); }}
      onSendBack={async (reason, feedback, attachments, answers) => {
        if (!user) return;
        // One route per act, chosen here rather than by a discriminator in the body,
        // so what happened is legible in a network log and in the server's own logs.
        const path = { project_id: projectId, task_id: taskId };
        const body = { user, feedback, attachments };
        // Answers ride with the ball move rather than on writes of their own, so the
        // agent never sees two of four questions answered (task-017).
        if (reason === "answer") await answer.mutateAsync({ path, body: { ...body, answers } });
        else if (reason === "redirect") await redirect.mutateAsync({ path, body });
        else if (reason === "hold") await hold.mutateAsync({ path, body });
        else await changes.mutateAsync({ path, body });
        await refresh();
      }}
      onReject={async (reason) => { if (!user) return; await reject.mutateAsync({ path: { project_id: projectId, task_id: taskId }, body: { user, reason } }); await navigate(`/p/${encodeURIComponent(projectId)}/tasks`, { replace: true }); }}
      fieldsBusy={update.isPending}
      fieldsError={fieldsError}
      fieldsVocabulary={vocabulary}
      onEditFields={() => setWantVocabulary(true)}
      onSaveFields={async (patch) => {
        if (!user) return;
        setFieldsError(null);
        try {
          // `expected_revision` is not optional here and is the whole reason a phone
          // edit is safe: an agent that wrote to this task since the page rendered
          // makes the patch a decision taken against content the person has not seen,
          // and the server refuses it rather than letting the older read win.
          //
          // `actor` says whose edit this is, and `operation_id` is what makes the
          // manager write the log entry naming the fields that moved -- without one
          // the patch lands silently, and an edit nobody can attribute is the failure
          // the append-only log exists to prevent.
          await update.mutateAsync({
            path: { project_id: projectId, task_id: taskId },
            query: { actor: user },
            body: { ...patch, expected_revision: revision, operation_id: crypto.randomUUID() },
          });
        } catch (error) {
          const refusal = readRefusal(error);
          if (refusal?.code === "revision_conflict") {
            // Re-read and re-present, exactly as promote does. Resending against the
            // new revision unasked would apply an edit to a record the person has not
            // seen, which is the thing the conflict is protecting them from.
            await refresh();
            setFieldsError("This task changed while you had it open, so nothing was saved.");
          } else {
            setFieldsError(
              refusal ? refusal.message : "The edit could not be saved. Reload the page and try again.",
            );
          }
          // Rethrown so the form stays open with the edits still in it.
          throw error;
        }
        await refresh();
      }}
      noteBusy={addNote.isPending}
      noteError={noteError}
      onAddNote={async (body) => {
        if (!user) return;
        setNoteError(null);
        try {
          // No expected_revision: appending is not a decision taken against content
          // that could have changed underneath it, so a concurrent write is not a
          // reason to throw the note away and make the human retype it.
          await addNote.mutateAsync({
            path: { project_id: projectId, task_id: taskId },
            body: { actor: user, type: "note", body },
          });
        } catch (error) {
          const refusal = readRefusal(error);
          setNoteError(refusal ? refusal.message : "The note could not be saved. Reload the page and try again.");
          // Rethrown so the composer keeps the form open with the text still in it. A
          // note that failed to save and vanished from the box is a note retyped.
          throw error;
        }
        await refresh();
      }}
      onPromote={async (note) => {
        if (!user) return;
        setPromoteError(null);
        try {
          // expected_revision comes from the loaded record, so a task edited from
          // another surface since this page rendered is refused rather than
          // overwritten.
          await promote.mutateAsync({ path: { project_id: projectId, task_id: taskId }, body: { actor: user, body: note, expected_revision: revision } });
          await refresh();
        } catch (error) {
          const refusal = readRefusal(error);
          if (refusal?.code === "revision_conflict") {
            // Re-read and re-present. Resending against the new revision without
            // being asked would promote a task the human has not seen.
            await refresh();
            setPromoteError("This task changed while the page was open, so it was not promoted. The record below has been reloaded — read it, then promote again if you still want to.");
            return;
          }
          setPromoteError(refusal ? refusal.message : "The promotion could not be recorded. Reload the page and try again.");
        }
      }}
    />
  );
}

/**
 * `/tasks/new`: the capture form as a page, with the specification already open.
 *
 * The same component the header's capture control opens, not a second authoring form
 * (task-346). The route stays because links to it do -- the Dashboard's "File a new
 * task", a bookmark, anything anybody pasted into a task record -- and because the
 * whole specification is easier to read on a page than in a dialog. What it is *not*
 * any more is the only way to reach those fields.
 */
function TaskCreatePage({ projectId }: { projectId: string }) {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const location = useLocation();
  const tasksQuery = useQuery(
    listTasksApiProjectsProjectIdTasksGetOptions({ path: { project_id: projectId } }),
  );
  // Who is filing this. The manager writes a creation log entry only when a creator is
  // named, so a create that omits it produces a task with an empty log -- no record of
  // who asked for it, and, since a dispatch must trace to a human's entry, a task that
  // can never be dispatched. Every task made in this browser had that shape until now.
  const projectsQuery = useQuery(getProjectsApiProjectsGetOptions());
  const create = useMutation(createTaskApiProjectsProjectIdTasksPostMutation());
  const destinations = (projectsQuery.data ?? []).map((entry) => ({
    id: entry.id,
    name: entry.name,
    reporter: entry.default_user ?? null,
  }));

  return (
    <section className="mx-auto max-w-3xl space-y-6" aria-labelledby="capture-page-heading">
      <header>
        <p className="text-sm font-semibold uppercase tracking-wide text-blue-300">New task</p>
        <h2 id="capture-page-heading" className="mt-1 text-3xl font-bold">
          Give the next reader enough to resume
        </h2>
        <p className="mt-2 text-dark-muted">
          The summary orients them; the working description tells them what to do.
        </p>
      </header>
      <CaptureForm
        context={readReportContext(location.pathname)}
        destinations={destinations}
        existingTaskIds={(tasksQuery.data ?? []).map((task) => task.id)}
        startExpanded
        onSubmit={async (destinationId, request) => {
          try {
            return await create.mutateAsync({
              path: { project_id: destinationId },
              body: request,
            });
          } catch (caught) {
            const refusal = readRefusal(caught);
            throw new Error(refusal ? refusal.message : "");
          }
        }}
        onFiled={(destinationId) => {
          void queryClient.invalidateQueries();
          navigate(`/p/${encodeURIComponent(destinationId)}/tasks?status=all`);
        }}
        cancel={
          <Link
            to={`/p/${encodeURIComponent(projectId)}/tasks`}
            className="touch-target rounded-lg px-4 font-semibold text-dark-muted hover:bg-dark-border"
          >
            Cancel
          </Link>
        }
      />
    </section>
  );
}

/**
 * The analytics page's one request (docs/analytics-design.md section 7.1).
 *
 * One endpoint for the whole page, not one per panel: the panels share a range, a
 * timezone and a coverage statement that must be identical across all of them, and
 * seventeen round trips over Tailscale would be seventeen chances to render half a
 * page. The range lives in this component rather than in the URL because it is a
 * reading position rather than a place -- a pasted link to the analytics page should
 * open on its default, and react-query caches each range separately so moving between
 * them costs nothing after the first look.
 */
function AnalyticsPage({ projectId }: { projectId: string }) {
  const [range, setRange] = useState(DEFAULT_ANALYTICS_RANGE);
  const analytics = useQuery(
    getAnalyticsApiProjectsProjectIdAnalyticsGetOptions({
      path: { project_id: projectId },
      query: { range },
    }),
  );
  return (
    <Analytics
      data={analytics.data ?? null}
      projectId={projectId}
      rangeKey={range}
      onRangeChange={setRange}
    />
  );
}

function PlaybooksPage({ projectId }: { projectId: string }) {
  const queryClient = useQueryClient();
  const [runningName, setRunningName] = useState<string | null>(null);
  const [refusal, setRefusal] = useState<{ name: string; refusal: DispatchRefusal } | null>(null);
  const [started, setStarted] = useState<{ name: string; taskId: string; created: boolean } | null>(null);

  const playbooksQuery = useQuery(
    getPlaybooksApiProjectsProjectIdPlaybooksGetOptions({ path: { project_id: projectId } }),
  );
  // The same endpoint the task page and the settings page read. One source for "may
  // this machine dispatch", so three surfaces cannot disagree about whether it can.
  const stateQuery = useQuery(
    getDispatchStateApiProjectsProjectIdDispatchGetOptions({ path: { project_id: projectId } }),
  );
  const run = useMutation(runPlaybookEndpointApiProjectsProjectIdPlaybooksNameRunPostMutation());

  const user = playbooksQuery.data?.identity.ok ? playbooksQuery.data.identity.user : null;

  return (
    <Playbooks
      collection={playbooksQuery.data ?? null}
      dispatchState={stateQuery.data ?? null}
      runningName={runningName}
      refusal={refusal}
      started={started}
      onRun={async ({ name, task }: PlaybookRunRequest): Promise<boolean> => {
        setRefusal(null);
        setStarted(null);
        setRunningName(name);
        try {
          // `user` is the human asking. For a project-target playbook the server
          // creates the run task attributed to them and that creation entry is the
          // authorisation; for a task-target one it is task-188's authorising entry on
          // the task named. Null is not sent, and the page disables the button first.
          const result = await run.mutateAsync({
            path: { project_id: projectId, name },
            body: { ...(user ? { user } : {}), ...(task ? { task } : {}) },
          });
          setStarted({ name, taskId: result.task_id, created: result.created_run_task });
          // A run task is a new task and a run changes the one it is aimed at, so the
          // lists this page links into are stale the moment this returns.
          await queryClient.invalidateQueries();
          return true;
        } catch (error) {
          const read = readRefusal(error);
          setRefusal({
            name,
            refusal: read
              ? { reason: read.code, message: read.message, suggestedAction: read.suggestedAction }
              : {
                  reason: "run_failed",
                  message: "The run could not be started. Reload the page and try again.",
                },
          });
          return false;
        } finally {
          setRunningName(null);
        }
      }}
    />
  );
}

/**
 * The header, with its two badges attached.
 *
 * A component of its own because each badge needs a hook and `PrimaryNav` must stay
 * prop-driven. The live-run query is shared with the Dashboard row and the Runs page
 * by react-query's cache, so a Dashboard costs one request rather than two.
 *
 * The two badges answer different questions and are deliberately not merged: the
 * green one is machine-wide and says what is running, the red one is this project's
 * and says what has stopped on you (task-338).
 */
function ProjectShellNav({
  projectId,
  attention,
  onAcknowledge,
}: {
  projectId: string;
  /**
   * Supplied by the shell rather than queried here, so the badge and the notifier read
   * one answer. React Query would have deduped the request; what it would not dedupe is
   * the two of them disagreeing for a render after an acknowledgment.
   */
  attention: AttentionResponse | null;
  onAcknowledge: (episodeId: string) => void;
}) {
  const episodeId = attention?.episode?.id;
  return (
    <PrimaryNav
      projectId={projectId}
      badge={<LiveRunCount body={useLiveRuns()} />}
      attention={
        <AttentionBadge
          count={attention?.blocking ?? null}
          projectId={projectId}
          onAcknowledge={() => {
            if (episodeId) onAcknowledge(episodeId);
          }}
        />
      }
    />
  );
}

/**
 * Whether the path inside the project shell is the Dashboard's own index route.
 *
 * Exported because the shell's whole layout turns on it, and the rule -- "there is no
 * segment after the project id" -- is worth asserting directly rather than through a
 * rendered page. `/p/x` and `/p/x/` are the Dashboard; `/p/x/tasks` is not.
 */
export function isDashboardPath(pathname: string, projectId: string): boolean {
  const base = `/p/${encodeURIComponent(projectId)}`;
  return pathname === base || pathname === `${base}/`;
}

/**
 * Whether the current URL is the Tasks surface, which is the one surface that becomes
 * two regions.
 *
 * `useMatch` against absolute patterns rather than the routes below, because a layout
 * route's `useParams` stops at its own segment and would never see `:taskId`. `new` is
 * excluded by hand: route ranking sends `/tasks/new` to `TaskCreatePage` -- a static
 * segment outscores a dynamic one -- but a raw pattern match has no ranking and would
 * call the Create page a task detail.
 */
function useTasksSurface(): boolean {
  const index = useMatch("/p/:projectId/tasks");
  const detail = useMatch("/p/:projectId/tasks/:taskId");
  return Boolean(index) || (detail !== null && detail.params.taskId !== "new");
}

/**
 * Which task record is open, or `null` on every other surface.
 *
 * The same `useMatch` the layout uses, asked for the id rather than for a boolean:
 * opening a task the attention episode names is one of the three acts that acknowledge
 * it (task-422), and that has to be decided from the URL rather than from inside the
 * detail page, which does not know what the episode holds.
 */
function useOpenTaskId(): string | null {
  const detail = useMatch("/p/:projectId/tasks/:taskId");
  const taskId = detail?.params.taskId ?? null;
  return taskId === "new" ? null : taskId;
}

function ProjectApp() {
  const { projectId = "" } = useParams<{ projectId: string }>();
  /**
   * Attention, read once for the whole shell (task-422).
   *
   * The badge renders it, the notifier drives the Windows taskbar and the desktop
   * notification from it, and the three acknowledging acts all post against the episode
   * id in it. One read, so none of those can be looking at a different answer.
   */
  const attention = useAttention(projectId);
  const acknowledgeAttention = useAcknowledgeAttention(projectId);
  useAcknowledgeFromUrl(acknowledgeAttention);
  useAcknowledgeOnOpen(attention, useOpenTaskId(), acknowledgeAttention);
  /**
   * Two surfaces are framed to exactly one viewport; every other one keeps the document
   * scroll.
   *
   * A frame of exactly one viewport, with `overflow-hidden` so nothing inside can push
   * the document past it, is what makes "never scrolls" a property of the layout rather
   * than a property of today's data. `dvh`, never `vh`: `100vh` on a phone is the height
   * *without* the retracting URL bar, so a `100vh` frame is taller than the visible
   * viewport and the page scrolls by exactly the bar's height. That is also why the
   * unframed shell is `min-h-dvh`.
   *
   * The Dashboard is framed at every viewport (task-294). The Tasks surface is framed
   * when it renders as two regions (task-237), so that each region scrolls itself and
   * reading a record cannot carry the list off the top of the screen; on the stacked
   * phone shell it is one thing at a time and the document scrolls as it always did.
   *
   * **task-294 deliberately did not frame the Tasks surface, and the reason it gave has
   * since been removed rather than overruled.** `dragAutoScroll.ts` scrolled the backlog
   * with `window.scrollBy`, which is a silent no-op the moment the document stops being
   * the scroller. It now finds the scrollable box around the row it was given and drives
   * that, so the frame no longer takes the gesture away.
   */
  const dashboardFrame = isDashboardPath(useLocation().pathname, projectId);
  // Both read unconditionally: `&&` would skip a hook on the Dashboard.
  const onTasksSurface = useTasksSurface();
  const wide = useWideShell();
  const twoRegion = onTasksSurface && wide;
  const framed = dashboardFrame || twoRegion;
  // `min-h-0` is the load-bearing half of both framed layouts. A flex child refuses to
  // shrink below its content by default, so without it the frame would be one viewport
  // tall and its contents would push straight through the bottom of it. The Tasks
  // surface also drops the centred `max-w-7xl`: two regions is what it uses the width
  // for, and the reading measure moves inside the detail region.
  let layout = "mx-auto max-w-7xl flex-1 py-8";
  if (dashboardFrame) layout = "mx-auto max-w-7xl flex min-h-0 flex-1 flex-col py-3";
  if (twoRegion) layout = "flex min-h-0 flex-1 flex-col py-4";
  return (
    <div
      className={`flex flex-col bg-dark-bg text-dark-text ${
        framed ? "h-dvh overflow-hidden" : "min-h-dvh"
      }`}
    >
      <ProjectShellNav
        projectId={projectId}
        attention={attention}
        onAcknowledge={acknowledgeAttention}
      />
      {/* Renders nothing. It drives the taskbar badge, the tab icon and the desktop
          notification, and it is mounted once here rather than per surface because the
          badge is a property of the window: two components setting it would fight. */}
      <AttentionNotifier projectId={projectId} attention={attention} />
      <main className={`w-full px-4 sm:px-6 lg:px-8 ${layout}`}>
        <LiveUpdateStatus projectId={projectId} />
        <Routes>
          <Route index element={<DashboardPage projectId={projectId} />} />
          <Route path="tasks/new" element={<TaskCreatePage projectId={projectId} />} />
          {/* One route with a child, not two siblings. The list region belongs to the
              parent and the record renders into its outlet, which is what lets a task
              open without the list unmounting. A deep-linked
              /p/{project}/tasks/{taskId} therefore arrives with both on screen. */}
          <Route path="tasks" element={<TasksSurface projectId={projectId} />}>
            <Route path=":taskId" element={<TaskDetailPage projectId={projectId} />} />
          </Route>
          {/* Unframed, like every surface but the Dashboard and the two-region Tasks
              view: a column of stacked panels is a document and may scroll. */}
          <Route path="analytics" element={<AnalyticsPage projectId={projectId} />} />
          <Route path="dispatch" element={<DispatchSettingsPage projectId={projectId} />} />
          <Route path="playbooks" element={<PlaybooksPage projectId={projectId} />} />
          {/* Inside the project shell for its chrome, machine-wide in its content:
              every row carries a server-built link into whichever project owns it. */}
          <Route path="runs" element={<LiveRunsRoute />} />
          <Route path="*" element={<Navigate to="/not-found" replace />} />
        </Routes>
      </main>
      {/* Dropped inside the frame. It is a copyright line with nothing reachable in it,
          and it costs 53px of a phone's 844 -- 14% of a phone in landscape. Every
          scrolling surface keeps it. */}
      {!framed && (
        <footer className="border-t border-dark-border bg-dark-surface"><div className="mx-auto max-w-7xl px-4 py-4 text-sm text-dark-muted sm:px-6 lg:px-8">AgentJobs © {new Date().getFullYear()}</div></footer>
      )}
    </div>
  );
}

function projectPath(projectId: string | undefined, path = "") {
  return `/p/${encodeURIComponent(projectId ?? "")}${path}`;
}

/**
 * A whole-screen state -- loading, unreachable, unsupported -- as one card.
 *
 * `min-h-full`, not `min-h-dvh`, and top-aligned rather than centred. These render
 * inside the Tasks surface's regions as well as on their own, and a region is exactly as
 * tall as the window: a card demanding a screen's worth of height inside one overflows
 * it, so opening the surface put a scrollbar on each region for as long as the two
 * queries took to answer, with the card floating in the middle of the empty space it had
 * made. Against an indefinite height `min-h-full` resolves to nothing, which is what the
 * standalone uses want.
 */
function StatusCard({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="mx-auto flex min-h-full max-w-xl items-start px-4 py-10">
      <section className="w-full rounded-2xl border border-dark-border bg-dark-surface p-6">
        <h1 className="text-2xl font-bold text-dark-text">{title}</h1>
        <div className="mt-3 text-dark-muted">{children}</div>
      </section>
    </div>
  );
}

export function App() {
  return (
    <>
      <Routes>
        <Route index element={<ProjectRedirect />} />
        <Route path="p/:projectId/*" element={<ProjectApp />} />
        <Route path="not-found" element={<StatusCard title="Page not found"><Link to="/">Return to AgentJobs</Link></StatusCard>} />
        <Route path="*" element={<Navigate to="/not-found" replace />} />
      </Routes>
      {/* Outside the routes on purpose: something is noticed on whatever page you are
          on, including the ones that render while no project has resolved yet. Inside
          the project shell the header carries the same control, and this renders
          nothing. */}
      <GlobalCapture />
      {/* Beside the routes for the same reason, and one more: a bundle talking to a
          server it was not built against can break the pages that render before any
          project resolves, so the warning cannot live inside one of them. */}
      <VersionSkew />
    </>
  );
}
