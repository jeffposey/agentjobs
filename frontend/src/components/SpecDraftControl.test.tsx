import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import type { Task } from "../api/generated";
import { client } from "../api/generated/client.gen";
import { apiMockServer } from "../test/api-mock";
import { TaskCreate } from "./TaskCreate";

/**
 * The drafting option on the create form, exercised through the form a person uses.
 *
 * Through `TaskCreate` rather than against `SpecDraftControl` on its own, because the
 * behaviour under test is not the control -- it is what happens to the *form fields*,
 * and a test that rendered the control alone would assert that a callback fired rather
 * than that the description box now holds what the model wrote.
 */

const CONFIGURED = {
  available: true,
  reason: null,
  detail: null,
  model: "a-model-id",
  calls_per_hour: 60,
  calls_used: 0,
};

const DRAFT = {
  drafted: true,
  summary: "Paging degrades once a project passes a few hundred tasks.",
  intent: "Filing more work should not make the backlog harder to read.",
  description: "## What to do\n\nPage the listing endpoint.",
  constraints: "No schema change.",
  out_of_scope: "Search.",
  acceptance: ["A project with 500 tasks renders its first page in under a second."],
  model: "a-model-id",
  reason: null,
  detail: null,
};

function createdTask(): Task {
  return {
    schema: 2,
    id: "task-123-created",
    title: "Created",
    created: "2026-09-20T08:00:00Z",
    updated: "2026-09-20T08:00:00Z",
    lifecycle: "draft",
    ball: "human",
    ball_reason: "decision",
    display_status: "Draft",
    priority: "medium",
    category: "general",
    assignment: { eligible: [] },
    spec: { summary: "s", description: "d" },
  };
}

function renderForm(onCreate = vi.fn().mockResolvedValue(createdTask())) {
  client.setConfig({ baseUrl: "http://localhost" });
  const queryClient = new QueryClient({
    defaultOptions: { mutations: { retry: false }, queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <TaskCreate projectId="agentjobs" existingTaskIds={[]} onCreate={onCreate} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return onCreate;
}

function configured(draft: unknown = DRAFT) {
  apiMockServer.use(
    http.get("*/api/model", () => HttpResponse.json(CONFIGURED)),
    http.post("*/api/projects/agentjobs/model/draft", () => HttpResponse.json(draft)),
  );
}

const checkbox = () => screen.getByRole("checkbox", { name: /Flesh this out with AI/ });
const field = (name: RegExp | string) => screen.getByRole("textbox", { name });

function type(name: RegExp | string, value: string) {
  fireEvent.change(field(name), { target: { value } });
}

async function pressDraft() {
  fireEvent.click(screen.getByRole("button", { name: "Draft the spec" }));
  await waitFor(() => expect(screen.queryByRole("button", { name: "Drafting…" })).toBeNull());
}

describe("drafting a spec on the create form", () => {
  it("offers the option, checked, once the machine has a model", async () => {
    configured();
    renderForm();

    await waitFor(() => expect(checkbox()).toBeEnabled());
    expect(checkbox()).toBeChecked();
    expect(screen.getByRole("button", { name: "Draft the spec" })).toBeInTheDocument();
  });

  it("fills the spec fields and saves nothing", async () => {
    configured();
    const onCreate = renderForm();

    await waitFor(() => expect(checkbox()).toBeEnabled());
    type("Title", "Paging is slow");
    type(/Working description/, "it drags at 400 tasks");
    await pressDraft();

    expect((field(/Summary/) as HTMLTextAreaElement).value).toBe(DRAFT.summary);
    expect((field(/Working description/) as HTMLTextAreaElement).value).toBe(DRAFT.description);
    expect((field("Intent") as HTMLTextAreaElement).value).toBe(DRAFT.intent);
    expect((field(/Acceptance criteria/) as HTMLTextAreaElement).value).toBe(
      DRAFT.acceptance.join("\n"),
    );
    // ac-3: the human still presses create, and until they do nothing is saved.
    expect(onCreate).not.toHaveBeenCalled();
  });

  it("opens the collapsed sections it wrote into", async () => {
    // Generated prose nobody can see is worse than none: the person has to read what
    // they are about to file.
    configured();
    renderForm();

    await waitFor(() => expect(checkbox()).toBeEnabled());
    type("Title", "Paging is slow");
    await pressDraft();

    expect(field("Intent")).toBeVisible();
    expect(field(/Acceptance criteria/)).toBeVisible();
  });

  it("says which of my own text it replaced, and puts it back on undo", async () => {
    // ac-4, demonstrated with a field that already had content.
    configured();
    renderForm();

    await waitFor(() => expect(checkbox()).toBeEnabled());
    type("Title", "Paging is slow");
    type(/Summary/, "The one true sentence I dictated.");
    type(/Working description/, "it drags at 400 tasks");
    await pressDraft();

    expect(screen.getByText(/Replaced what you had written in Summary/)).toBeInTheDocument();
    expect((field(/Summary/) as HTMLTextAreaElement).value).toBe(DRAFT.summary);

    fireEvent.click(screen.getByRole("button", { name: "Undo the draft" }));

    expect((field(/Summary/) as HTMLTextAreaElement).value).toBe(
      "The one true sentence I dictated.",
    );
    expect((field(/Working description/) as HTMLTextAreaElement).value).toBe(
      "it drags at 400 tasks",
    );
  });

  it("does not touch anything the model has no business setting", async () => {
    // ac-5 through the form: a server answering with a priority and a parent moves
    // neither, because there is nowhere for them to land.
    configured({
      ...DRAFT,
      priority: "critical",
      parent: "task-001-invented",
      lifecycle: "ready",
    });
    renderForm();

    await waitFor(() => expect(checkbox()).toBeEnabled());
    type("Title", "Paging is slow");
    await pressDraft();

    expect(screen.getByRole("radio", { name: /Draft/ })).toBeChecked();
    expect((screen.getByLabelText(/Parent task/) as HTMLInputElement).value).toBe("");
    expect((screen.getByLabelText(/Priority/) as HTMLSelectElement).value).toBe("medium");
  });

  it("shows a refusal without breaking the form", async () => {
    configured({
      drafted: false,
      summary: "",
      intent: "",
      description: "",
      constraints: "",
      out_of_scope: "",
      acceptance: [],
      model: null,
      reason: "rate_limited",
      detail: "The model provider is rate-limiting this machine. Try again shortly.",
    });
    renderForm();

    await waitFor(() => expect(checkbox()).toBeEnabled());
    type("Title", "Paging is slow");
    type(/Summary/, "Mine.");
    await pressDraft();

    expect(await screen.findByRole("alert")).toHaveTextContent(/rate-limiting/);
    expect((field(/Summary/) as HTMLTextAreaElement).value).toBe("Mine.");
  });

  it("lets me turn it off and file the task by hand", async () => {
    configured();
    const onCreate = renderForm();

    await waitFor(() => expect(checkbox()).toBeEnabled());
    fireEvent.click(checkbox());

    expect(screen.queryByRole("button", { name: "Draft the spec" })).toBeNull();
    type("Title", "By hand");
    type(/Summary/, "Written by me.");
    type(/Working description/, "Also written by me.");
    fireEvent.click(screen.getByRole("button", { name: "Create task" }));

    await waitFor(() => expect(onCreate).toHaveBeenCalled());
    expect(onCreate.mock.calls[0][0].summary).toBe("Written by me.");
  });
});

describe("when no model is configured", () => {
  // The default `/api/model` handler answers `unconfigured`, which is the state of
  // every machine that has not opted in -- including the one this was written on.

  it("says why, and leaves the form exactly as it is today", async () => {
    const onCreate = renderForm();

    // Waiting on the sentence rather than on the disabled state: the control starts
    // disabled before the status arrives, so a test that waited on that alone would
    // pass without the server ever having answered.
    expect(await screen.findByText(/No model is configured on this machine/)).toBeInTheDocument();
    expect(checkbox()).toBeDisabled();
    expect(checkbox()).not.toBeChecked();
    expect(screen.queryByRole("button", { name: "Draft the spec" })).toBeNull();

    type("Title", "Filed with no model anywhere");
    type(/Summary/, "A summary.");
    type(/Working description/, "A description.");
    fireEvent.click(screen.getByRole("button", { name: "Create task" }));

    await waitFor(() => expect(onCreate).toHaveBeenCalled());
    expect(onCreate.mock.calls[0][0].title).toBe("Filed with no model anywhere");
  });
});
