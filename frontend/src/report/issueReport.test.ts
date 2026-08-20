import { describe, expect, it } from "vitest";

import { buildIssueTaskRequest, readReportContext, REPORTED_ISSUE_TAG } from "./issueReport";

const draft = { title: "  Filters match nothing  ", details: "  Every filter returns zero rows.  ", actionable: false };

describe("readReportContext", () => {
  it("reads the project and task from a task detail route", () => {
    expect(readReportContext("/p/agentjobs/tasks/task-052-react-app")).toEqual({
      route: "/p/agentjobs/tasks/task-052-react-app",
      projectId: "agentjobs",
      taskId: "task-052-react-app",
    });
  });

  it("reads the project alone from any other project route", () => {
    expect(readReportContext("/p/alpha/tasks")).toEqual({
      route: "/p/alpha/tasks",
      projectId: "alpha",
      taskId: null,
    });
    expect(readReportContext("/p/alpha")).toEqual({
      route: "/p/alpha",
      projectId: "alpha",
      taskId: null,
    });
  });

  it("reports no project on a page that has none, rather than guessing one", () => {
    expect(readReportContext("/")).toEqual({ route: "/", projectId: null, taskId: null });
    expect(readReportContext("/not-found")).toEqual({
      route: "/not-found",
      projectId: null,
      taskId: null,
    });
  });

  it("decodes a project id that had to be escaped in the URL", () => {
    expect(readReportContext("/p/my%20project/tasks").projectId).toBe("my project");
  });

  it("does not mistake the create page for a task", () => {
    // "/p/x/tasks/new" is the create form, not a task called "new". Linking it as a
    // related dependency would produce a permanently dangling reference.
    expect(readReportContext("/p/agentjobs/tasks/new")).toEqual({
      route: "/p/agentjobs/tasks/new",
      projectId: "agentjobs",
      taskId: null,
    });
  });
});

describe("buildIssueTaskRequest", () => {
  const context = { route: "/p/agentjobs/tasks/task-052", projectId: "agentjobs", taskId: "task-052" };

  it("creates a tagged draft attributed to the reporter, linking the task they were viewing", () => {
    const request = buildIssueTaskRequest({
      draft,
      context,
      destinationProjectId: "agentjobs",
      reporter: "Jeff Posey",
      operationId: "op-1",
    });

    expect(request.title).toBe("Filters match nothing");
    expect(request.lifecycle).toBe("draft");
    expect(request.tags).toEqual([REPORTED_ISSUE_TAG]);
    expect(request.actor).toBe("Jeff Posey");
    expect(request.operation_id).toBe("op-1");
    expect(request.dependencies).toEqual([
      { task: "task-052", type: "related", note: "Reported while viewing this task." },
    ]);
    expect(request.description).toContain("Every filter returns zero rows.");
    expect(request.description).toContain("/p/agentjobs/tasks/task-052");
    expect(request.description).toContain("Jeff Posey");
    expect(request.description).toContain("task-052");
  });

  it("marks the task ready when the reporter says it is actionable as it stands", () => {
    const request = buildIssueTaskRequest({
      draft: { ...draft, actionable: true },
      context,
      destinationProjectId: "agentjobs",
      reporter: "Jeff Posey",
      operationId: "op-2",
    });
    expect(request.lifecycle).toBe("ready");
  });

  it("does not link the viewed task across projects, and says so in the record", () => {
    // A dependency is resolved within one project's corpus, so pointing at a task id
    // from a different project would be a permanently unmet edge. The fact still has
    // to survive, so it goes into the description in words.
    const request = buildIssueTaskRequest({
      draft,
      context,
      destinationProjectId: "alpha",
      reporter: "Jeff Posey",
      operationId: "op-3",
    });

    expect(request.dependencies).toEqual([]);
    expect(request.description).toContain("task-052");
    expect(request.description).toContain("agentjobs");
    expect(request.description).toContain("not the project this issue was filed into");
  });

  it("records the project being viewed even when no task was open", () => {
    const request = buildIssueTaskRequest({
      draft,
      context: { route: "/p/agentjobs/tasks", projectId: "agentjobs", taskId: null },
      destinationProjectId: "alpha",
      reporter: "Jeff Posey",
      operationId: "op-4",
    });
    expect(request.dependencies).toEqual([]);
    expect(request.description).toContain("Noticed while viewing project `agentjobs`");
  });
});
