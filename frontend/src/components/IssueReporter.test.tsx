import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import type { TaskCreateRequest } from "../api/types";
import { client } from "../api/generated/client.gen";
import { MAX_ATTACHMENT_BYTES } from "../report/attachments";
import { apiMockServer } from "../test/api-mock";
import { IssueReporter } from "./IssueReporter";

function project(id: string, name: string, user: string | null) {
  return {
    id,
    name,
    root: `C:/projects/${id}`,
    task_count: 3,
    tasks_directory: `C:/projects/${id}/tasks`,
    actors: user ? [{ id: user, kind: "human", display_name: user }] : [],
    default_user: user,
  };
}

function renderReporter(route: string) {
  const queryClient = new QueryClient({
    defaultOptions: { mutations: { retry: false }, queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[route]}>
        <IssueReporter />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

async function openReporter(route: string) {
  renderReporter(route);
  fireEvent.click(screen.getByRole("button", { name: "Report issue" }));
  await screen.findByRole("dialog", { name: "Report an issue" });
}

function fill(title: string, details: string) {
  fireEvent.change(screen.getByRole("textbox", { name: /^Title/ }), { target: { value: title } });
  fireEvent.change(screen.getByRole("textbox", { name: /^What happened/ }), {
    target: { value: details },
  });
}

function pasteImage(target: HTMLElement, file: File) {
  // The interaction the feature is about: focus the box, Ctrl+V. A clipboard paste of
  // a screenshot arrives as an image blob in `clipboardData.items`, with no file
  // picker anywhere in the path.
  fireEvent.paste(target, {
    clipboardData: { items: [{ kind: "file", getAsFile: () => file }], files: [] },
  });
}

describe("IssueReporter", () => {
  it("files a tagged, attributed task carrying the page and the task being viewed", async () => {
    client.setConfig({ baseUrl: "http://localhost" });
    let received: TaskCreateRequest | null = null;
    apiMockServer.use(
      http.get("*/api/projects", () =>
        HttpResponse.json([project("agentjobs", "AgentJobs", "Jeff Posey")]),
      ),
      http.post("*/api/projects/agentjobs/tasks", async ({ request }) => {
        received = (await request.json()) as TaskCreateRequest;
        return HttpResponse.json({ id: "task-140-filters" }, { status: 201 });
      }),
    );

    await openReporter("/p/agentjobs/tasks/task-052-react-app");
    fill("Filters match nothing", "Every task-list filter returns zero rows.");
    fireEvent.click(screen.getByRole("button", { name: "File issue" }));

    await screen.findByText("task-140-filters");
    const body = received as unknown as TaskCreateRequest;
    expect(body.title).toBe("Filters match nothing");
    expect(body.tags).toEqual(["reported-issue"]);
    expect(body.actor).toBe("Jeff Posey");
    expect(body.lifecycle).toBe("draft");
    expect(body.operation_id).toBeTruthy();
    expect(body.dependencies).toEqual([
      { task: "task-052-react-app", type: "related", note: "Reported while viewing this task." },
    ]);
    expect(body.description).toContain("/p/agentjobs/tasks/task-052-react-app");
  });

  it("is reachable on the project picker, where no project is in scope yet", async () => {
    client.setConfig({ baseUrl: "http://localhost" });
    let received: TaskCreateRequest | null = null;
    apiMockServer.use(
      http.get("*/api/projects", () =>
        HttpResponse.json([project("agentjobs", "AgentJobs", "Jeff Posey")]),
      ),
      http.post("*/api/projects/agentjobs/tasks", async ({ request }) => {
        received = (await request.json()) as TaskCreateRequest;
        return HttpResponse.json({ id: "task-141" }, { status: 201 });
      }),
    );

    await openReporter("/");
    // The projects list has to arrive before the destination can resolve.
    await screen.findByRole("combobox", { name: "File into project" });
    fill("Opening AgentJobs hangs", "The picker never resolves a project.");
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "File issue" })).not.toBeDisabled(),
    );
    fireEvent.click(screen.getByRole("button", { name: "File issue" }));

    await screen.findByText("task-141");
    const body = received as unknown as TaskCreateRequest;
    expect(body.dependencies).toEqual([]);
    expect(body.description).toContain("`/`");
  });

  it("files into the chosen project rather than the one being read", async () => {
    client.setConfig({ baseUrl: "http://localhost" });
    let received: TaskCreateRequest | null = null;
    apiMockServer.use(
      http.get("*/api/projects", () =>
        HttpResponse.json([
          project("agentjobs", "AgentJobs", "Jeff Posey"),
          project("alpha", "Alpha", "Jeff Posey"),
        ]),
      ),
      http.post("*/api/projects/agentjobs/tasks", async ({ request }) => {
        received = (await request.json()) as TaskCreateRequest;
        return HttpResponse.json({ id: "task-142" }, { status: 201 });
      }),
    );

    await openReporter("/p/alpha/tasks/task-004-import");
    const destination = await screen.findByRole("combobox", { name: "File into project" });
    expect(destination).toHaveValue("alpha");
    fireEvent.change(destination, { target: { value: "agentjobs" } });
    fill("The task list scrolls sideways", "Horizontal scroll on a phone.");
    fireEvent.click(screen.getByRole("button", { name: "File issue" }));

    await screen.findByText("task-142");
    const body = received as unknown as TaskCreateRequest;
    expect(body.dependencies).toEqual([]);
    expect(body.description).toContain("not the project this issue was filed into");
  });

  it("attaches a pasted screenshot and sends it with the report", async () => {
    client.setConfig({ baseUrl: "http://localhost" });
    let received: TaskCreateRequest | null = null;
    apiMockServer.use(
      http.get("*/api/projects", () =>
        HttpResponse.json([project("agentjobs", "AgentJobs", "Jeff Posey")]),
      ),
      http.post("*/api/projects/agentjobs/tasks", async ({ request }) => {
        received = (await request.json()) as TaskCreateRequest;
        return HttpResponse.json({ id: "task-143" }, { status: 201 });
      }),
    );

    await openReporter("/p/agentjobs/tasks");
    fill("The badge shows an enum name", "Look at the status column.");
    pasteImage(
      screen.getByRole("textbox", { name: /^What happened/ }),
      new File([new Uint8Array(8)], "screenshot.png", { type: "image/png" }),
    );

    const gallery = await screen.findByRole("list", { name: "Attached images" });
    expect(within(gallery).getByRole("img", { name: "screenshot.png" })).toBeVisible();

    fireEvent.click(screen.getByRole("button", { name: "File issue" }));
    await screen.findByText("task-143");
    const body = received as unknown as TaskCreateRequest;
    expect(body.attachments).toHaveLength(1);
    expect(body.attachments?.[0]?.label).toBe("screenshot.png");
    expect(body.attachments?.[0]?.data_base64).toBeTruthy();
    // Base64 belongs in the request, never in the record: the server writes a sidecar.
    expect(body.description).not.toContain("base64");
  });

  it("rejects an oversized paste without touching what the reporter typed", async () => {
    client.setConfig({ baseUrl: "http://localhost" });
    apiMockServer.use(
      http.get("*/api/projects", () =>
        HttpResponse.json([project("agentjobs", "AgentJobs", "Jeff Posey")]),
      ),
    );

    await openReporter("/p/agentjobs/tasks");
    fill("Something is wrong", "Prose that must survive a rejected image.");
    pasteImage(
      screen.getByRole("textbox", { name: /^What happened/ }),
      new File([new Uint8Array(MAX_ATTACHMENT_BYTES + 1)], "huge.png", { type: "image/png" }),
    );

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("over the");
    expect(screen.getByRole("textbox", { name: /^What happened/ })).toHaveValue(
      "Prose that must survive a rejected image.",
    );
    expect(screen.queryByRole("list", { name: "Attached images" })).toBeNull();
  });

  it("refuses to file when the destination project names no single human", async () => {
    client.setConfig({ baseUrl: "http://localhost" });
    apiMockServer.use(
      http.get("*/api/projects", () => HttpResponse.json([project("agentjobs", "AgentJobs", null)])),
    );

    await openReporter("/p/agentjobs/tasks");
    fill("Something is wrong", "Details.");
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "File issue" })).toBeDisabled(),
    );
    expect(screen.getByRole("alert").textContent).toContain("No single human actor is configured");
  });

  it("keeps the reporter on the page and reports a refusal without losing the draft", async () => {
    client.setConfig({ baseUrl: "http://localhost" });
    apiMockServer.use(
      http.get("*/api/projects", () =>
        HttpResponse.json([project("agentjobs", "AgentJobs", "Jeff Posey")]),
      ),
      http.post("*/api/projects/agentjobs/tasks", () =>
        HttpResponse.json(
          { code: "unknown_actor", message: "'Jeff Posey' is not an actor in this project." },
          { status: 400 },
        ),
      ),
    );

    await openReporter("/p/agentjobs/tasks");
    fill("Filters match nothing", "Every filter returns zero rows.");
    fireEvent.click(screen.getByRole("button", { name: "File issue" }));

    await screen.findByRole("alert");
    expect(screen.getByRole("alert").textContent).toContain("is not an actor in this project");
    expect(screen.getByRole("textbox", { name: /^Title/ })).toHaveValue("Filters match nothing");
  });

  it("drafts a spec into the reporter, and files it as an ordinary task", async () => {
    // The reporter composes with `buildIssueTaskRequest` rather than bypassing it, so
    // a drafted report is one task filed by one path -- same tag, same provenance, same
    // attribution as one typed in fifteen seconds.
    client.setConfig({ baseUrl: "http://localhost" });
    let received: TaskCreateRequest | null = null;
    apiMockServer.use(
      http.get("*/api/projects", () =>
        HttpResponse.json([project("agentjobs", "AgentJobs", "Jeff Posey")]),
      ),
      http.get("*/api/model", () =>
        HttpResponse.json({
          available: true,
          reason: null,
          detail: null,
          model: "a-model-id",
          calls_per_hour: 60,
          calls_used: 0,
        }),
      ),
      http.post("*/api/projects/agentjobs/model/draft", () =>
        HttpResponse.json({
          drafted: true,
          summary: "Every task-list filter matches nothing.",
          intent: "A filter that silently matches nothing is worse than no filter.",
          description: "## What happens\n\nSelecting any filter empties the list.",
          constraints: "",
          out_of_scope: "",
          acceptance: ["Selecting a ball filter returns the tasks with that ball."],
          model: "a-model-id",
          reason: null,
          detail: null,
        }),
      ),
      http.post("*/api/projects/agentjobs/tasks", async ({ request }) => {
        received = (await request.json()) as TaskCreateRequest;
        return HttpResponse.json({ id: "task-141-filters" }, { status: 201 });
      }),
    );

    await openReporter("/p/agentjobs/tasks");
    fill("Filters match nothing", "every filter returns zero rows");

    const checkbox = await screen.findByRole("checkbox", { name: /Flesh this out with AI/ });
    await waitFor(() => expect(checkbox).toBeEnabled());
    expect(checkbox).toBeChecked();
    fireEvent.click(screen.getByRole("button", { name: "Draft the spec" }));

    await screen.findByRole("textbox", { name: /^Summary/ });
    expect(screen.getByRole("textbox", { name: /^Summary/ })).toHaveValue(
      "Every task-list filter matches nothing.",
    );
    expect(screen.getByRole("textbox", { name: /^What happened/ })).toHaveValue(
      "## What happens\n\nSelecting any filter empties the list.",
    );

    fireEvent.click(screen.getByRole("button", { name: "File issue" }));
    await screen.findByText("task-141-filters");

    const body = received as unknown as TaskCreateRequest;
    expect(body.summary).toBe("Every task-list filter matches nothing.");
    expect(body.intent).toBe("A filter that silently matches nothing is worse than no filter.");
    expect(body.acceptance).toEqual([
      {
        id: "ac-1",
        text: "Selecting a ball filter returns the tasks with that ball.",
        status: "pending",
      },
    ]);
    // ac-7: nothing marks it as drafted. It is tagged and attributed exactly as a
    // hand-typed report is, and the reporter's own checkbox still sets the lifecycle.
    expect(body.tags).toEqual(["reported-issue"]);
    expect(body.actor).toBe("Jeff Posey");
    expect(body.lifecycle).toBe("draft");
    expect(JSON.stringify(body)).not.toContain("a-model-id");
  });

  it("offers no drafting option when no model is configured", async () => {
    client.setConfig({ baseUrl: "http://localhost" });
    apiMockServer.use(
      http.get("*/api/projects", () =>
        HttpResponse.json([project("agentjobs", "AgentJobs", "Jeff Posey")]),
      ),
    );

    await openReporter("/p/agentjobs/tasks");
    expect(await screen.findByText(/No model is configured on this machine/)).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: /Flesh this out with AI/ })).toBeDisabled();
    expect(screen.queryByRole("button", { name: "Draft the spec" })).toBeNull();
  });
});
