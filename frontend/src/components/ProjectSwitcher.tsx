import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";

import { getProjectsApiProjectsGetOptions } from "../api/generated/@tanstack/react-query.gen";

function projectPath(projectId: string) {
  return `/p/${encodeURIComponent(projectId)}`;
}

export function ProjectSwitcher({ projectId }: { projectId: string }) {
  const navigate = useNavigate();
  const projectsQuery = useQuery(getProjectsApiProjectsGetOptions());
  const projects = projectsQuery.data ?? [];
  const activeProject = projects.find((project) => project.id === projectId);

  if (projectsQuery.isPending) {
    return (
      <span
        aria-label="Loading projects"
        className="touch-target max-w-48 min-w-0 rounded-md border border-dark-border bg-dark-bg px-3 text-sm text-dark-muted"
      >
        {projectId}
      </span>
    );
  }

  if (projectsQuery.isError || projects.length === 0) {
    return (
      <span
        aria-label="Current project"
        className="touch-target max-w-48 min-w-0 rounded-md border border-dark-border bg-dark-bg px-3 text-sm text-dark-muted"
      >
        {projectId}
      </span>
    );
  }

  return (
    <label className="min-w-0 max-w-56">
      <span className="sr-only">Project</span>
      <select
        aria-label="Project"
        value={activeProject?.id ?? ""}
        onChange={(event) => navigate(projectPath(event.target.value))}
        className="touch-target w-full min-w-0 rounded-md border border-dark-border bg-dark-bg px-3 text-sm font-semibold text-dark-text focus:border-blue-500 focus:outline-none"
      >
        {!activeProject && <option value="">Unknown project: {projectId}</option>}
        {projects.map((project) => (
          <option value={project.id} key={project.id}>{project.name}</option>
        ))}
      </select>
    </label>
  );
}
