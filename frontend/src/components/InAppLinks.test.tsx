import { render } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import type { DashboardResponse, TaskRead } from "../api/types";
import { Dashboard } from "./Dashboard";
import { TaskList } from "./TaskList";

/**
 * The React app is mounted with `basename="/app"` (see main.tsx), so every in-app
 * path builder deliberately omits that prefix and relies on react-router to add it.
 * A raw `<a href>` is not routed, so it silently drops the basename and navigates to
 * the legacy Jinja UI at the same path -- a full page load out of the application,
 * with no error and no visible difference until the user notices the page has stopped
 * updating.
 *
 * That shipped, in the task list, and cost a day of misdiagnosis (task-006, task-008).
 * These tests assert the rendered href rather than the source, because what matters is
 * what the browser does with the markup.
 *
 * NOTE: the pre-existing component tests render with a bare `<MemoryRouter>`, which has
 * no basename and therefore cannot catch this. Rendering WITH a basename is the whole
 * point here.
 */

const BASENAME = "/app";

function task(id: string, overrides: Partial<TaskRead> = {}): TaskRead {
  return {
    schema: 2,
    id,
    title: `Title of ${id}`,
    created: "2026-08-13T08:00:00Z",
    updated: "2026-08-13T09:00:00Z",
    lifecycle: "ready",
    ball: "agent",
    ball_reason: "available",
    display_status: "Ready",
    priority: "medium",
    category: "general",
    tags: [],
    assignment: { eligible: [] },
    spec: { summary: `Summary of ${id}`, description: "Body." },
    ...overrides,
  };
}

function dashboard(overrides: Partial<DashboardResponse> = {}): DashboardResponse {
  return {
    stats: {
      total: 1,
      in_progress: 0,
      blocked: 0,
      waiting_for_human: 0,
      awaiting_input: 0,
      completed: 0,
    },
    active_tasks: [],
    recent_updates: [],
    waiting_tasks: [],
    backlog_tasks: [],
    next_task: null,
    next_action: "nothing_claimable",
    broken_files: [],
    ...overrides,
  };
}

/** Hrefs the browser would treat as same-origin application paths. */
function internalHrefs(container: HTMLElement): Array<string> {
  return [...container.querySelectorAll("a[href]")]
    .map((anchor) => anchor.getAttribute("href") ?? "")
    .filter((href) => href.startsWith("/"));
}

/**
 * `/docs` is the FastAPI documentation. It genuinely lives outside the React app and is
 * correctly a raw anchor, so it is not expected to carry the basename.
 */
const EXTERNAL_TO_THE_APP = ["/docs"];

function assertEveryInAppLinkIsRouted(container: HTMLElement) {
  const internal = internalHrefs(container);
  const inApp = internal.filter((href) => !EXTERNAL_TO_THE_APP.includes(href));

  // Not vacuous: if a change removes the links entirely, this fails rather than passing.
  expect(inApp.length).toBeGreaterThan(0);

  const escaped = inApp.filter((href) => !href.startsWith(`${BASENAME}/`));
  expect(
    escaped,
    `These hrefs leave the React app and land on the legacy Jinja UI. They were rendered ` +
      `without the "${BASENAME}" basename, which means a raw <a href> was used where a ` +
      `react-router <Link> is required.`,
  ).toEqual([]);
}

describe("in-app links keep the router basename", () => {
  it("task list rows link inside the app", () => {
    const { container } = render(
      <MemoryRouter basename={BASENAME} initialEntries={["/app/p/inbox/tasks"]}>
        <TaskList tasks={[task("task-001"), task("task-002")]} brokenFiles={[]} projectId="inbox" />
      </MemoryRouter>,
    );

    assertEveryInAppLinkIsRouted(container);
  });

  it("a task list row points at the React task detail page, prefix included", () => {
    const { container } = render(
      <MemoryRouter basename={BASENAME} initialEntries={["/app/p/inbox/tasks"]}>
        <TaskList tasks={[task("task-001")]} brokenFiles={[]} projectId="inbox" />
      </MemoryRouter>,
    );

    const hrefs = internalHrefs(container);
    expect(hrefs).toContain("/app/p/inbox/tasks/task-001");
  });

  it("dashboard links stay inside the app", () => {
    const { container } = render(
      <MemoryRouter basename={BASENAME} initialEntries={["/app/p/inbox"]}>
        <Dashboard
          dashboard={dashboard({
            active_tasks: [task("task-001", { lifecycle: "active", display_status: "In flight" })],
            waiting_tasks: [
              task("task-002", {
                lifecycle: "active",
                ball: "human",
                ball_reason: "review",
                ball_prompt: "Approve or request changes.",
                display_status: "Waiting for review",
              }),
            ],
          })}
          projectId="inbox"
        />
      </MemoryRouter>,
    );

    assertEveryInAppLinkIsRouted(container);
  });

  it("leaves genuinely external links alone", () => {
    const { container } = render(
      <MemoryRouter basename={BASENAME} initialEntries={["/app/p/inbox"]}>
        <Dashboard dashboard={dashboard()} projectId="inbox" />
      </MemoryRouter>,
    );

    // /docs is served by FastAPI outside the React app; prefixing it would break it.
    for (const href of internalHrefs(container).filter((h) => h.startsWith("/docs"))) {
      expect(href).toBe("/docs");
    }
  });
});
