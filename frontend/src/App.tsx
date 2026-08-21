import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, Navigate, Route, Routes, useNavigate, useParams } from "react-router-dom";

import {
  appendLogEntryApiProjectsProjectIdTasksTaskIdLogPostMutation,
  cancelDispatchRunApiProjectsProjectIdDispatchRunsRunIdCancelPostMutation,
  disableDispatchApiProjectsProjectIdDispatchDisablePostMutation,
  enableDispatchApiProjectsProjectIdDispatchEnablePostMutation,
  getDashboardApiProjectsProjectIdDashboardGetOptions,
  getDispatchStateApiProjectsProjectIdDispatchGetOptions,
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
  queueMoveTaskApiProjectsProjectIdTasksTaskIdQueueMovePostMutation,
  rejectTaskApiProjectsProjectIdTasksTaskIdRejectPostMutation,
  reprioritizeTaskApiProjectsProjectIdTasksTaskIdReprioritizePostMutation,
  answerTaskApiProjectsProjectIdTasksTaskIdAnswerPostMutation,
  holdTaskApiProjectsProjectIdTasksTaskIdHoldPostMutation,
  redirectTaskApiProjectsProjectIdTasksTaskIdRedirectPostMutation,
  requestChangesApiProjectsProjectIdTasksTaskIdRequestChangesPostMutation,
  resumeTaskApiProjectsProjectIdTasksTaskIdResumePostMutation,
} from "./api/generated/@tanstack/react-query.gen";
import type { DispatchRunView, Priority } from "./api/types";
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
  type DispatchRefusal,
} from "./components/DispatchPanel";
import { DispatchRunOutput } from "./components/DispatchOutput";
import { TaskList, type ReorderHandlers } from "./components/TaskList";
import { TaskDetail } from "./components/TaskDetail";
import { TaskCreate } from "./components/TaskCreate";
import { IssueReporter } from "./components/IssueReporter";
import { NextExplanation } from "./components/NextExplanation";
import { invalidateProjectTaskQueries, LiveUpdateStatus } from "./components/LiveUpdates";
import { ProjectSwitcher } from "./components/ProjectSwitcher";

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

  return (
    <Dashboard
      dashboard={dashboardQuery.data}
      projectId={projectId}
      renderWhyThisOne={() => <NextExplanation projectId={projectId} />}
    />
  );
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
          await move.mutateAsync({
            path: { project_id: projectId, task_id: taskId },
            body: { actor, operation_id: crypto.randomUUID(), ...placement },
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
          : "Reordering is off because this project configures no human actor, and every queue move is recorded against one. Add a default_user to .agentjobs/config.yaml."
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
    onDispatch: async (note?: string): Promise<boolean> => {
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
          body: { ...(user ? { user } : {}), ...(note ? { note } : {}) },
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
      onEnable={async (runner) => {
        setError(null);
        try {
          await enable.mutateAsync({ path: { project_id: projectId }, body: { runner } });
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
      onApprove={async () => { if (!user) return; await approve.mutateAsync({ path: { project_id: projectId, task_id: taskId }, body: { user } }); await refresh(); }}
      onResume={async (note) => { if (!user) return; await resume.mutateAsync({ path: { project_id: projectId, task_id: taskId }, body: { user, note } }); await refresh(); }}
      onSendBack={async (reason, feedback, attachments) => {
        if (!user) return;
        // One route per act, chosen here rather than by a discriminator in the body,
        // so what happened is legible in a network log and in the server's own logs.
        const path = { project_id: projectId, task_id: taskId };
        const body = { user, feedback, attachments };
        if (reason === "answer") await answer.mutateAsync({ path, body });
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

function ProjectApp() {
  const { projectId = "" } = useParams<{ projectId: string }>();
  return (
    <div className="flex min-h-screen flex-col bg-dark-bg text-dark-text">
      <header className="border-b border-dark-border bg-dark-surface">
        <nav className="mx-auto flex min-h-16 max-w-7xl flex-wrap items-center gap-2 px-4 py-2 min-[820px]:gap-6 sm:px-6 lg:px-8" aria-label="Primary navigation">
          <h1 className="text-2xl font-bold">AgentJobs</h1>
          <ProjectSwitcher projectId={projectId} />
          <Link to={projectPath(projectId)} className="touch-target rounded-md px-3 text-sm font-medium hover:bg-dark-border">Dashboard</Link>
          <Link to={projectPath(projectId, "/tasks")} className="touch-target rounded-md px-3 text-sm font-medium hover:bg-dark-border">Tasks</Link>
          <Link to={projectPath(projectId, "/tasks/new")} className="touch-target rounded-md px-3 text-sm font-medium text-blue-300 hover:bg-dark-border">Create</Link>
          {/* Its own nav entry, not buried in a menu: this is where the switch that
              stops every future run lives, and a kill switch you cannot reach is not one. */}
          <Link to={projectPath(projectId, "/dispatch")} className="touch-target rounded-md px-3 text-sm font-medium hover:bg-dark-border">Dispatch</Link>
          <a href="/docs" className="touch-target rounded-md px-3 text-sm font-medium hover:bg-dark-border">API Docs</a>
        </nav>
      </header>
      <main className="mx-auto w-full max-w-7xl flex-1 px-4 py-8 sm:px-6 lg:px-8">
        <LiveUpdateStatus projectId={projectId} />
        <Routes>
          <Route index element={<DashboardPage projectId={projectId} />} />
          <Route path="tasks" element={<TaskListPage projectId={projectId} />} />
          <Route path="tasks/new" element={<TaskCreatePage projectId={projectId} />} />
          <Route path="tasks/:taskId" element={<TaskDetailPage projectId={projectId} />} />
          <Route path="dispatch" element={<DispatchSettingsPage projectId={projectId} />} />
          <Route path="*" element={<Navigate to="/not-found" replace />} />
        </Routes>
      </main>
      <footer className="border-t border-dark-border bg-dark-surface"><div className="mx-auto max-w-7xl px-4 py-4 text-sm text-dark-muted sm:px-6 lg:px-8">AgentJobs © {new Date().getFullYear()}</div></footer>
    </div>
  );
}

function projectPath(projectId: string | undefined, path = "") {
  return `/p/${encodeURIComponent(projectId ?? "")}${path}`;
}

function StatusCard({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <main className="mx-auto flex min-h-screen max-w-xl items-center px-4 py-10">
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
