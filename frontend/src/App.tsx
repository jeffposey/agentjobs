import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, Navigate, Route, Routes, useLocation, useNavigate, useParams } from "react-router-dom";

import {
  appendLogEntryApiProjectsProjectIdTasksTaskIdLogPostMutation,
  cancelDispatchRunApiProjectsProjectIdDispatchRunsRunIdCancelPostMutation,
  disableDispatchApiProjectsProjectIdDispatchDisablePostMutation,
  enableDispatchApiProjectsProjectIdDispatchEnablePostMutation,
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
} from "./api/generated/@tanstack/react-query.gen";
import type { DispatchRunView, MutationResultOutput, Priority } from "./api/types";
import { readRefusal } from "./api/mutation-error";
import {
  requireSupportedTaskSchemas,
  UnsupportedTaskSchemaError,
} from "./api/schema-version";
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
import { TaskList, type ReorderHandlers } from "./components/TaskList";
import { TaskDetail } from "./components/TaskDetail";
import { TaskCreate } from "./components/TaskCreate";
import { IssueReporter } from "./components/IssueReporter";
import { NextExplanation } from "./components/NextExplanation";
import { invalidateProjectTaskQueries, LiveUpdateStatus } from "./components/LiveUpdates";
import { LiveRunCount, LiveRunsPage, useLiveRuns } from "./components/LiveRuns";
import { Playbooks, type PlaybookRunRequest } from "./components/Playbooks";
import { AttentionBadge, useHumanAttention } from "./components/AttentionBadge";
import { PrimaryNav } from "./components/PrimaryNav";
import { QueueDispatch, QueueDispatchGate } from "./components/QueueDispatch";
import { SlotBoard } from "./components/SlotBoard";

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
              // The same expression the task page uses, against the same field the
              // server checks. Drift between the two costs a link instead of a button,
              // never a dispatch the server would refuse.
              canBrief={Boolean(task.spec.description?.trim())}
              taskHref={`/p/${encodeURIComponent(projectId)}/tasks/${encodeURIComponent(task.id)}`}
              busy={dispatch.startingTaskId === task.id}
              refusal={dispatch.refusal?.taskId === task.id ? dispatch.refusal.refusal : null}
              onDispatch={() => void dispatch.start(task.id, identity.ok ? identity.user : null)}
            />
          )}
          renderQueueGate={() => (
            <QueueDispatchGate state={dispatch.state} projectId={projectId} />
          )}
        />
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
  const [refusal, setRefusal] = useState<{ taskId: string; refusal: DispatchRefusal } | null>(null);

  // The same endpoint the task page, the playbooks page and the settings page read, so
  // no two surfaces can disagree about whether this machine may dispatch.
  const stateQuery = useQuery(
    getDispatchStateApiProjectsProjectIdDispatchGetOptions({ path: { project_id: projectId } }),
  );
  const start = useMutation(
    dispatchTaskEndpointApiProjectsProjectIdTasksTaskIdDispatchPostMutation(),
  );

  return {
    state: stateQuery.data ?? null,
    startingTaskId,
    refusal,
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
  return <LiveRunsPage body={useLiveRuns()} />;
}

function TaskListPage({ projectId }: { projectId: string }) {
  const queryClient = useQueryClient();
  const tasksQuery = useQuery({
    ...listTasksApiProjectsProjectIdTasksGetOptions({ path: { project_id: projectId } }),
    select: (tasks) => {
      requireSupportedTaskSchemas(tasks);
      return tasks;
    },
  });
  const brokenQuery = useQuery(
    listBrokenTasksApiProjectsProjectIdTasksBrokenGetOptions({ path: { project_id: projectId } }),
  );
  // Read for `problems` and `repair_command` alone -- the order itself comes with the
  // tasks. This endpoint reports rather than raising, which is exactly why the banner
  // reads it: it is the one queue surface that still answers while the queue is broken.
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
  if (tasksQuery.isPending || brokenQuery.isPending) return <StatusCard title="Opening tasks...">Loading current task data.</StatusCard>;
  if (!tasksQuery.data || !brokenQuery.data) return <ConnectionUnavailable offline={false} />;

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
      brokenFiles={brokenQuery.data}
      projectId={projectId}
      queueProblems={queueQuery.data?.problems ?? []}
      repairCommand={queueQuery.data?.repair_command ?? "agentjobs queue repair"}
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
        await start.mutateAsync({
          path: { project_id: projectId, task_id: taskId },
          // `options` is spread rather than picked apart: its keys are absent unless
          // the human chose something, so a dispatch with nothing picked posts the
          // same body it posted before the group pulldown existed, and task-307's
          // posture arrives here without touching this call.
          body: { ...(user ? { user } : {}), ...(options ?? {}) },
        });
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
  const after = async () => { await queryClient.invalidateQueries(); };

  return (
    <DispatchSettings
      state={stateQuery.data ?? null}
      busy={enable.isPending || disable.isPending}
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
      dispatch={dispatch}
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

function TaskCreatePage({ projectId }: { projectId: string }) {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const tasksQuery = useQuery(
    listTasksApiProjectsProjectIdTasksGetOptions({ path: { project_id: projectId } }),
  );
  // Who is filing this. The manager writes a creation log entry only when a creator is
  // named, so a create that omits it produces a task with an empty log -- no record of
  // who asked for it, and, since a dispatch must trace to a human's entry, a task that
  // can never be dispatched. Every task made in this browser had that shape until now.
  const projectsQuery = useQuery(getProjectsApiProjectsGetOptions());
  const author = projectsQuery.data?.find((entry) => entry.id === projectId)?.default_user ?? null;
  const create = useMutation(createTaskApiProjectsProjectIdTasksPostMutation());

  return (
    <TaskCreate
      projectId={projectId}
      existingTaskIds={(tasksQuery.data ?? []).map((task) => task.id)}
      onCreate={async (request) => {
        const task = await create.mutateAsync({
          path: { project_id: projectId },
          body: { ...request, actor: author },
        });
        await queryClient.invalidateQueries();
        navigate(`/p/${encodeURIComponent(projectId)}/tasks?status=all`);
        return task;
      }}
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
function ProjectShellNav({ projectId }: { projectId: string }) {
  return (
    <PrimaryNav
      projectId={projectId}
      badge={<LiveRunCount body={useLiveRuns()} />}
      attention={<AttentionBadge count={useHumanAttention(projectId)} projectId={projectId} />}
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

function ProjectApp() {
  const { projectId = "" } = useParams<{ projectId: string }>();
  /**
   * The Dashboard is framed; every other surface keeps the document scroll (task-294).
   *
   * A frame of exactly one viewport, with `overflow-hidden` so nothing inside can push
   * the document past it, is what makes "never scrolls" a property of the layout rather
   * than a property of today's data. It is applied to this one route rather than to the
   * shell as a whole because `dragAutoScroll.ts` scrolls the backlog with
   * `window.scrollBy`, which becomes a silent no-op the moment the document stops being
   * the scroller -- and the Tasks surface is the one that renders it. The decision entry
   * on task-294 records the alternative and why it was not taken.
   *
   * `dvh`, never `vh`: `100vh` on a phone is the height *without* the retracting URL
   * bar, so a `100vh` frame is taller than the visible viewport and the page scrolls by
   * exactly the bar's height. That is also why the unframed shell is `min-h-dvh` now.
   */
  const framed = isDashboardPath(useLocation().pathname, projectId);
  return (
    <div
      className={`flex flex-col bg-dark-bg text-dark-text ${
        framed ? "h-dvh overflow-hidden" : "min-h-dvh"
      }`}
    >
      <ProjectShellNav projectId={projectId} />
      <main
        className={`mx-auto w-full max-w-7xl px-4 sm:px-6 lg:px-8 ${
          // `min-h-0` is the load-bearing half. A flex child refuses to shrink below
          // its content by default, so without it the frame would be one viewport tall
          // and its contents would push straight through the bottom of it.
          framed ? "flex min-h-0 flex-1 flex-col py-3" : "flex-1 py-8"
        }`}
      >
        <LiveUpdateStatus projectId={projectId} />
        <Routes>
          <Route index element={<DashboardPage projectId={projectId} />} />
          <Route path="tasks" element={<TaskListPage projectId={projectId} />} />
          <Route path="tasks/new" element={<TaskCreatePage projectId={projectId} />} />
          <Route path="tasks/:taskId" element={<TaskDetailPage projectId={projectId} />} />
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

function StatusCard({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <main className="mx-auto flex min-h-dvh max-w-xl items-center px-4 py-10">
      <section className="w-full rounded-2xl border border-dark-border bg-dark-surface p-6">
        <h1 className="text-2xl font-bold text-dark-text">{title}</h1>
        <div className="mt-3 text-dark-muted">{children}</div>
      </section>
    </main>
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
      {/* Outside the routes on purpose: a finding is noticed on whatever page you are
          on, including the ones that render while no project has resolved yet. */}
      <IssueReporter />
    </>
  );
}
