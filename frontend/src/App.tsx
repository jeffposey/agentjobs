import { useEffect, useState } from "react";
import { Link, Navigate, Route, Routes, useNavigate, useParams } from "react-router-dom";

type Project = {
  id: string;
  name: string;
};

type TaskSummary = {
  id: string;
};

function ProjectRedirect() {
  const navigate = useNavigate();
  const [state, setState] = useState<"loading" | "empty" | "error">("loading");

  useEffect(() => {
    const controller = new AbortController();

    async function resolveProject() {
      try {
        const response = await fetch("/api/projects", { signal: controller.signal });
        if (!response.ok) {
          throw new Error(`Project lookup failed with ${response.status}`);
        }
        const projects = (await response.json()) as Project[];
        if (projects.length === 0) {
          setState("empty");
          return;
        }
        navigate(`/p/${encodeURIComponent(projects[0].id)}`, { replace: true });
      } catch {
        if (!controller.signal.aborted) {
          setState("error");
        }
      }
    }

    void resolveProject();
    return () => controller.abort();
  }, [navigate]);

  if (state === "empty") {
    return (
      <StatusCard title="No projects are registered">
        <p>Register or create a project before opening the React app.</p>
        <a className="mt-4 inline-block font-semibold text-blue-300 hover:text-blue-200" href="/projects/new">
          Add or create a project
        </a>
      </StatusCard>
    );
  }

  if (state === "error") {
    return (
      <StatusCard title="AgentJobs could not load the project registry">
        <p>Confirm the local server is running, then reload this page.</p>
      </StatusCard>
    );
  }

  return <StatusCard title="Opening AgentJobs…">Resolving the first registered project.</StatusCard>;
}

function ApiProof() {
  const { projectId } = useParams<{ projectId: string }>();
  const [taskCount, setTaskCount] = useState<number | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    setTaskCount(null);
    setFailed(false);

    async function loadTasks() {
      try {
        const response = await fetch(
          `/api/projects/${encodeURIComponent(projectId ?? "")}/tasks`,
          { signal: controller.signal },
        );
        if (!response.ok) {
          throw new Error(`Task lookup failed with ${response.status}`);
        }
        const tasks = (await response.json()) as TaskSummary[];
        setTaskCount(tasks.length);
      } catch {
        if (!controller.signal.aborted) {
          setFailed(true);
        }
      }
    }

    void loadTasks();
    return () => controller.abort();
  }, [projectId]);

  return (
    <main className="mx-auto flex min-h-screen max-w-3xl items-center px-4 py-10 sm:px-6">
      <section className="w-full rounded-2xl border border-dark-border bg-dark-surface p-6 shadow-xl sm:p-10">
        <p className="text-sm font-semibold uppercase tracking-[0.2em] text-blue-300">React foundation</p>
        <h1 className="mt-3 text-3xl font-bold text-dark-text sm:text-4xl">AgentJobs is connected</h1>
        <p className="mt-4 break-all text-dark-muted">
          Project <span className="font-mono text-dark-text">{projectId}</span>
        </p>
        <div className="mt-8 rounded-xl border border-dark-border bg-dark-bg p-5">
          {failed ? (
            <p className="text-red-300">The scoped task API could not be reached.</p>
          ) : taskCount === null ? (
            <p className="text-dark-muted">Loading real project data…</p>
          ) : (
            <p className="text-dark-text">
              The scoped API returned <strong>{taskCount}</strong> {taskCount === 1 ? "task" : "tasks"}.
            </p>
          )}
        </div>
        <p className="mt-6 text-sm leading-6 text-dark-muted">
          This connection proof is intentionally the only page in phase 1. Product screens follow in later tasks.
        </p>
      </section>
    </main>
  );
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
    <Routes>
      <Route index element={<ProjectRedirect />} />
      <Route path="p/:projectId/*" element={<ApiProof />} />
      <Route path="not-found" element={<StatusCard title="Page not found"><Link to="/">Return to AgentJobs</Link></StatusCard>} />
      <Route path="*" element={<Navigate to="/not-found" replace />} />
    </Routes>
  );
}
