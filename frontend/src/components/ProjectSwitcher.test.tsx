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
    id: "alpha",
    name: "Alpha",
    root: "C:/projects/alpha",
    task_count: 4,
    tasks_directory: "C:/projects/alpha/tasks/alpha",
  },
  {
    id: "beta",
    name: "Beta",
    root: "C:/projects/beta",
    task_count: 12,
    tasks_directory: "C:/projects/beta/tasks/beta",
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
      <MemoryRouter initialEntries={["/p/alpha/tasks/task-001"]}>
        <ProjectSwitcher projectId="alpha" />
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
    expect(selector).toHaveValue("alpha");
    expect(screen.getAllByRole("option").map((option) => option.textContent)).toEqual([
      "AgentJobs",
      "Alpha",
      "Beta",
    ]);

    fireEvent.change(selector, { target: { value: "beta" } });

    await waitFor(() => {
      expect(screen.getByRole("status", { name: "Current location" })).toHaveTextContent("/p/beta");
    });
  });
});
