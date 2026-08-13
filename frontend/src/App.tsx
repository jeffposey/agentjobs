import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, Navigate, Route, Routes, useNavigate, useParams } from "react-router-dom";

import {
  getDashboardApiProjectsProjectIdDashboardGetOptions,
  getProjectsApiProjectsGetOptions,
  getTaskDetailApiProjectsProjectIdTasksTaskIdDetailGetOptions,
  approveTaskApiProjectsProjectIdTasksTaskIdApprovePostMutation,
  listBrokenTasksApiProjectsProjectIdTasksBrokenGetOptions,
  listTasksApiProjectsProjectIdTasksGetOptions,
  rejectTaskApiProjectsProjectIdTasksTaskIdRejectPostMutation,
  requestChangesApiProjectsProjectIdTasksTaskIdRequestChangesPostMutation,
} from "./api/generated/@tanstack/react-query.gen";
import {
  requireSupportedTaskSchemas,
  UnsupportedTaskSchemaError,
} from "./api/schema-version";
import { Dashboard } from "./components/Dashboard";
import { ConnectionUnavailable } from "./components/ConnectionUnavailable";
import { TaskList } from "./components/TaskList";
import { TaskDetail } from "./components/TaskDetail";

function useOnlineStatus() {
  const [online, setOnline] = useState(() => navigator.onLine);
  useEffect(() => {
    const update = () => setOnline(navigator.onLine);
    window.addEventListener("online", update);
    window.addEventListener("offline", update);
    return () => {
      window.removeEventListener("online", update);
      window.removeEventListener("offline", update);
    };
  }, []);
  return online;
}

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

  if (dashboardQuery.isError) {
    return <ConnectionUnavailable offline={false} />;
  }

  return <Dashboard dashboard={dashboardQuery.data} projectId={projectId} />;
}

function TaskListPage({ projectId }: { projectId: string }) {
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

  if (tasksQuery.error instanceof UnsupportedTaskSchemaError) {
    return <StatusCard title="Unsupported task schema"><p>{tasksQuery.error.message}</p><p className="mt-4">Upgrade the UI before viewing this project.</p></StatusCard>;
  }
  if (tasksQuery.isPending || brokenQuery.isPending) return <StatusCard title="Opening tasks...">Loading current task data.</StatusCard>;
  if (tasksQuery.isError || brokenQuery.isError) return <ConnectionUnavailable offline={false} />;
  return <TaskList tasks={tasksQuery.data} brokenFiles={brokenQuery.data} projectId={projectId} />;
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
  const reject = useMutation(rejectTaskApiProjectsProjectIdTasksTaskIdRejectPostMutation());

  if (detailQuery.error instanceof UnsupportedTaskSchemaError) return <StatusCard title="Unsupported task schema">{detailQuery.error.message}</StatusCard>;
  if (detailQuery.isPending) return <StatusCard title="Opening task...">Loading the complete task record.</StatusCard>;
  if (detailQuery.isError) return <StatusCard title="Task could not be loaded">Confirm the task still exists, then return to the list.</StatusCard>;
  const user = detailQuery.data.identity.user;
  const refresh = async () => { await queryClient.invalidateQueries(); };
  const actionError = approve.error || changes.error || reject.error;
  return (
    <TaskDetail
      detail={detailQuery.data}
      projectId={projectId}
      busy={approve.isPending || changes.isPending || reject.isPending}
      error={actionError ? "The action could not be recorded. Reload and try again." : null}
      onApprove={async () => { if (!user) return; await approve.mutateAsync({ path: { project_id: projectId, task_id: taskId }, body: { user } }); await refresh(); }}
      onRequestChanges={async (feedback) => { if (!user) return; await changes.mutateAsync({ path: { project_id: projectId, task_id: taskId }, body: { user, feedback } }); await refresh(); }}
      onReject={async (reason) => { if (!user) return; await reject.mutateAsync({ path: { project_id: projectId, task_id: taskId }, body: { user, reason } }); await navigate(`/p/${encodeURIComponent(projectId)}/tasks`, { replace: true }); }}
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
          <span className="hidden rounded-md border border-dark-border bg-dark-bg px-3 py-2 font-mono text-xs text-dark-muted sm:block">{projectId}</span>
          <Link to={projectPath(projectId)} className="touch-target rounded-md px-3 text-sm font-medium hover:bg-dark-border">Dashboard</Link>
          <Link to={projectPath(projectId, "/tasks")} className="touch-target rounded-md px-3 text-sm font-medium hover:bg-dark-border">Tasks</Link>
          <a href="/docs" className="touch-target rounded-md px-3 text-sm font-medium hover:bg-dark-border">API Docs</a>
        </nav>
      </header>
      <main className="mx-auto w-full max-w-7xl flex-1 px-4 py-8 sm:px-6 lg:px-8">
        <Routes>
          <Route index element={<DashboardPage projectId={projectId} />} />
          <Route path="tasks" element={<TaskListPage projectId={projectId} />} />
          <Route path="tasks/:taskId" element={<TaskDetailPage projectId={projectId} />} />
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
  const online = useOnlineStatus();
  if (!online) return <ConnectionUnavailable offline />;
  return (
    <Routes>
      <Route index element={<ProjectRedirect />} />
      <Route path="p/:projectId/*" element={<ProjectApp />} />
      <Route path="not-found" element={<StatusCard title="Page not found"><Link to="/">Return to AgentJobs</Link></StatusCard>} />
      <Route path="*" element={<Navigate to="/not-found" replace />} />
    </Routes>
  );
}
