import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { MemoryRouter, useLocation } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { client } from "../api/generated/client.gen";
import { apiMockServer } from "../test/api-mock";
import { ProjectSwitcher } from "./ProjectSwitcher";

const projects = [
  {
    id: "agentjobs",
    name: "AgentJobs",
    root: "C:/projects/agentjobs",
    task_count: 80,
    tasks_directory: "C:/projects/agentjobs/tasks/agentjobs",
  },
  {
    id: "job-hunting",
    name: "Job Hunting",
    root: "C:/projects/job-hunting",
    task_count: 4,
    tasks_directory: "C:/projects/job-hunting/tasks/job-hunting",
  },
  {
    id: "mastercalls",
    name: "Mastercalls",
    root: "C:/projects/mastercalls",
    task_count: 12,
    tasks_directory: "C:/projects/mastercalls/tasks/mastercalls",
  },
];

function CurrentLocation() {
  const location = useLocation();
  return <output aria-label="Current location">{location.pathname}</output>;
}

function renderSwitcher() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/p/job-hunting/tasks/task-001"]}>
        <ProjectSwitcher projectId="job-hunting" />
        <CurrentLocation />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("ProjectSwitcher", () => {
  it("loads every registered project, marks the active one, and switches to a project dashboard", async () => {
    client.setConfig({ baseUrl: "http://localhost" });
    apiMockServer.use(
      http.get("*/api/projects", () => HttpResponse.json(projects)),
    );
    renderSwitcher();

    const selector = await screen.findByRole("combobox", { name: "Project" });
    expect(selector).toHaveValue("job-hunting");
    expect(screen.getAllByRole("option").map((option) => option.textContent)).toEqual([
      "AgentJobs",
      "Job Hunting",
      "Mastercalls",
    ]);

    fireEvent.change(selector, { target: { value: "mastercalls" } });

    await waitFor(() => {
      expect(screen.getByRole("status", { name: "Current location" })).toHaveTextContent("/p/mastercalls");
    });
  });
});
