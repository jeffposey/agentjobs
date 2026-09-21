import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it } from "vitest";

import type { TaskCreateRequest } from "../api/types";
import { client } from "../api/generated/client.gen";
import { MAX_ATTACHMENT_BYTES } from "../report/attachments";
import { draftStore, memoryDraftStore, setDraftStore } from "../report/draftStore";
import { apiMockServer } from "../test/api-mock";
import { CaptureControl } from "./CaptureControl";

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

function renderControl(route: string) {
  const queryClient = new QueryClient({
    defaultOptions: { mutations: { retry: false }, queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[route]}>
        <CaptureControl />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

async function openCapture(route: string) {
  renderControl(route);
  fireEvent.click(screen.getByRole("button", { name: "New task or issue" }));
  await screen.findByRole("dialog", { name: "New task" });
  // The projects list decides the destination, and the destination decides who is
  // filing -- so until it arrives the form correctly refuses to file anything. Waiting
  // for the picker to hold an option is waiting for that, rather than for a timer.
  await waitFor(() =>
    expect(
      screen.getByRole("combobox", { name: "File into project" }).children.length,
    ).toBeGreaterThan(0),
  );
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

describe("CaptureControl", () => {
  beforeEach(() => {
    // A fresh device per test. The draft store is shared for the lifetime of the tab,
    // which is right in a browser and wrong in a file of tests: a draft left by one
    // would be restored into the next one's form.
    setDraftStore(memoryDraftStore());
  });

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

    await openCapture("/p/agentjobs/tasks/task-052-react-app");
    fill("Filters match nothing", "Every task-list filter returns zero rows.");
    fireEvent.click(screen.getByRole("button", { name: "File it" }));

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

    await openCapture("/");
    fill("Opening AgentJobs hangs", "The picker never resolves a project.");
    fireEvent.click(screen.getByRole("button", { name: "File it" }));

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

    await openCapture("/p/alpha/tasks/task-004-import");
    const destination = screen.getByRole("combobox", { name: "File into project" });
    expect(destination).toHaveValue("alpha");
    fireEvent.change(destination, { target: { value: "agentjobs" } });
    fill("The task list scrolls sideways", "Horizontal scroll on a phone.");
    fireEvent.click(screen.getByRole("button", { name: "File it" }));

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

    await openCapture("/p/agentjobs/tasks");
    fill("The badge shows an enum name", "Look at the status column.");
    pasteImage(
      screen.getByRole("textbox", { name: /^What happened/ }),
      new File([new Uint8Array(8)], "screenshot.png", { type: "image/png" }),
    );

    const gallery = await screen.findByRole("list", { name: "Attached images" });
    expect(within(gallery).getByRole("img", { name: "screenshot.png" })).toBeVisible();

    fireEvent.click(screen.getByRole("button", { name: "File it" }));
    await screen.findByText("task-143");
    const body = received as unknown as TaskCreateRequest;
    expect(body.attachments).toHaveLength(1);
    expect(body.attachments?.[0]?.label).toBe("screenshot.png");
    expect(body.attachments?.[0]?.data_base64).toBeTruthy();
    // Base64 belongs in the request, never in the record: the server writes a sidecar.
    expect(body.description).not.toContain("base64");
  });

  it("rejects an oversized paste without touching what was typed", async () => {
    client.setConfig({ baseUrl: "http://localhost" });
    apiMockServer.use(
      http.get("*/api/projects", () =>
        HttpResponse.json([project("agentjobs", "AgentJobs", "Jeff Posey")]),
      ),
    );

    await openCapture("/p/agentjobs/tasks");
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

    await openCapture("/p/agentjobs/tasks");
    fill("Something is wrong", "Details.");
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "File it" })).toBeDisabled(),
    );
    expect(screen.getByRole("alert").textContent).toContain("No single human actor is configured");
  });

  it("stays open and reports a refusal without losing what was typed", async () => {
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

    await openCapture("/p/agentjobs/tasks");
    fill("Filters match nothing", "Every filter returns zero rows.");
    fireEvent.click(screen.getByRole("button", { name: "File it" }));

    await screen.findByRole("alert");
    expect(screen.getByRole("alert").textContent).toContain("is not an actor in this project");
    expect(screen.getByRole("textbox", { name: /^Title/ })).toHaveValue("Filters match nothing");
  });

  it("drafts a spec into the capture, and files it as an ordinary task", async () => {
    // The form composes with `buildCaptureRequest` rather than bypassing it, so a
    // drafted capture is one task filed by one path -- same tag, same provenance, same
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
      // A draft opens the specification, and the Parent field's completions are
      // fetched the moment it does.
      http.get("*/api/projects/agentjobs/tasks", () => HttpResponse.json([])),
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

    await openCapture("/p/agentjobs/tasks");
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

    fireEvent.click(screen.getByRole("button", { name: "File it" }));
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

  it("returns focus to the trigger when the dialog closes", async () => {
    client.setConfig({ baseUrl: "http://localhost" });
    apiMockServer.use(
      http.get("*/api/projects", () =>
        HttpResponse.json([project("agentjobs", "AgentJobs", "Jeff Posey")]),
      ),
    );

    await openCapture("/p/agentjobs/tasks");
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    // By hand, because this is not a browser dialog: the node that had focus has left
    // the document, and without this the next Tab starts from the top of the page.
    expect(screen.getByRole("button", { name: "New task or issue" })).toHaveFocus();
  });

  it("reaches the whole specification from the same dialog, without refiling", async () => {
    client.setConfig({ baseUrl: "http://localhost" });
    let received: TaskCreateRequest | null = null;
    apiMockServer.use(
      http.get("*/api/projects", () =>
        HttpResponse.json([project("agentjobs", "AgentJobs", "Jeff Posey")]),
      ),
      http.get("*/api/projects/agentjobs/tasks", () => HttpResponse.json([])),
      http.post("*/api/projects/agentjobs/tasks", async ({ request }) => {
        received = (await request.json()) as TaskCreateRequest;
        return HttpResponse.json({ id: "task-144" }, { status: 201 });
      }),
    );

    await openCapture("/p/agentjobs/tasks");
    fill("Worth specifying after all", "It turned out to be bigger than a note.");
    fireEvent.click(screen.getByRole("button", { name: "Add the full specification" }));

    // task-342: priority is reachable from a capture without abandoning it and
    // starting again in a different form.
    fireEvent.change(await screen.findByRole("combobox", { name: "Priority" }), {
      target: { value: "high" },
    });
    fireEvent.change(screen.getByRole("textbox", { name: /Acceptance criteria/ }), {
      target: { value: "The filter returns the tasks it names." },
    });
    fireEvent.click(screen.getByRole("button", { name: "File it" }));

    await screen.findByText("task-144");
    const body = received as unknown as TaskCreateRequest;
    expect(body.priority).toBe("high");
    expect(body.acceptance).toEqual([
      { id: "ac-1", text: "The filter returns the tasks it names.", status: "pending" },
    ]);
    // Still one population: expanding does not turn it into a different kind of record.
    expect(body.tags).toEqual(["reported-issue"]);
  });

  it("offers no drafting option when no model is configured", async () => {
    client.setConfig({ baseUrl: "http://localhost" });
    apiMockServer.use(
      http.get("*/api/projects", () =>
        HttpResponse.json([project("agentjobs", "AgentJobs", "Jeff Posey")]),
      ),
    );

    await openCapture("/p/agentjobs/tasks");
    expect(await screen.findByText(/No model is configured on this machine/)).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: /Flesh this out with AI/ })).toBeDisabled();
    expect(screen.queryByRole("button", { name: "Draft the spec" })).toBeNull();
  });

  it("puts back what was being typed when the dialog is opened again", async () => {
    // The durability the tray has had since task-121 and the form had not: everything
    // typed before Ctrl+Enter lived only in React state, so the page going away for any
    // reason took it. Closing the dialog is the cheapest way to make the page go away.
    client.setConfig({ baseUrl: "http://localhost" });
    apiMockServer.use(
      http.get("*/api/projects", () =>
        HttpResponse.json([project("agentjobs", "AgentJobs", "Jeff Posey")]),
      ),
      http.get("*/api/projects/agentjobs/tasks", () => HttpResponse.json([])),
    );

    await openCapture("/p/agentjobs/tasks");
    fill("Half a finding", "Typed and not yet collected.");
    // The specification too, which is the half held in the DOM rather than in state.
    fireEvent.click(screen.getByRole("button", { name: "Add the full specification" }));
    fireEvent.change(await screen.findByRole("textbox", { name: /^Summary/ }), {
      target: { value: "One sentence for a reader with no context." },
    });
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());

    fireEvent.click(screen.getByRole("button", { name: "New task or issue" }));
    await screen.findByRole("dialog", { name: "New task" });
    await waitFor(() =>
      expect(screen.getByRole("textbox", { name: /^Title/ })).toHaveValue("Half a finding"),
    );
    expect(screen.getByRole("textbox", { name: /^What happened/ })).toHaveValue(
      "Typed and not yet collected.",
    );
    // The section comes back open, because a restored field nobody can see is a field
    // that will be filed without being read.
    expect(screen.getByRole("textbox", { name: /^Summary/ })).toHaveValue(
      "One sentence for a reader with no context.",
    );
  });

  it("keeps no draft for a dialog that was opened and typed in and emptied", async () => {
    client.setConfig({ baseUrl: "http://localhost" });
    apiMockServer.use(
      http.get("*/api/projects", () =>
        HttpResponse.json([project("agentjobs", "AgentJobs", "Jeff Posey")]),
      ),
    );

    await openCapture("/p/agentjobs/tasks");
    fill("Never mind", "Nor this.");
    fill("", "");
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());

    expect(await draftStore().load("capture")).toBeNull();
  });

  it("forgets the draft the moment the finding is on the tray", async () => {
    // ac-3, and the one duplicate this feature could produce: a finding that is on the
    // tray *and* offered back as something still to type would be filed twice by
    // somebody working quickly.
    client.setConfig({ baseUrl: "http://localhost" });
    apiMockServer.use(
      http.get("*/api/projects", () =>
        HttpResponse.json([project("agentjobs", "AgentJobs", "Jeff Posey")]),
      ),
    );

    await openCapture("/p/agentjobs/tasks");
    fill("The dashboard cards overlap", "At 375px they sit on top of each other.");
    fireEvent.keyDown(screen.getByRole("textbox", { name: /^Title/ }), {
      key: "Enter",
      ctrlKey: true,
    });

    const tray = await screen.findByRole("region", { name: "Collected findings" });
    expect(within(tray).getByText("The dashboard cards overlap")).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByRole("textbox", { name: /^Title/ })).toHaveValue(""),
    );
    expect(await draftStore().load("capture")).toBeNull();

    // And reopening offers one finding on the tray with an empty form, not two findings.
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    fireEvent.click(screen.getByRole("button", { name: /New task or issue/ }));
    await screen.findByRole("dialog", { name: "New task" });
    const restored = await screen.findByRole("region", { name: "Collected findings" });
    expect(within(restored).getAllByText("The dashboard cards overlap")).toHaveLength(1);
    expect(screen.getByRole("textbox", { name: /^Title/ })).toHaveValue("");
  });

  it("forgets the draft once the server has the task", async () => {
    client.setConfig({ baseUrl: "http://localhost" });
    apiMockServer.use(
      http.get("*/api/projects", () =>
        HttpResponse.json([project("agentjobs", "AgentJobs", "Jeff Posey")]),
      ),
      http.post("*/api/projects/agentjobs/tasks", () =>
        HttpResponse.json({ id: "task-145" }, { status: 201 }),
      ),
    );

    await openCapture("/p/agentjobs/tasks");
    fill("Filed, not drafted", "This one went straight to the server.");
    fireEvent.click(screen.getByRole("button", { name: "File it" }));

    await screen.findByText("task-145");
    expect(await draftStore().load("capture")).toBeNull();
  });

  it("still files from a browser that will not keep a draft at all", async () => {
    // ac-4. jsdom has no IndexedDB, which is exactly the state of a private window or a
    // browser with site data blocked -- so this is the real fallback, not a simulation
    // of one. What is lost is durability; what is not lost is the capture.
    setDraftStore(null);
    expect(draftStore().durable).toBe(false);

    client.setConfig({ baseUrl: "http://localhost" });
    let received: TaskCreateRequest | null = null;
    apiMockServer.use(
      http.get("*/api/projects", () =>
        HttpResponse.json([project("agentjobs", "AgentJobs", "Jeff Posey")]),
      ),
      http.post("*/api/projects/agentjobs/tasks", async ({ request }) => {
        received = (await request.json()) as TaskCreateRequest;
        return HttpResponse.json({ id: "task-146" }, { status: 201 });
      }),
    );

    await openCapture("/p/agentjobs/tasks");
    fill("Filed without durability", "No store, and the form still works.");
    fireEvent.click(screen.getByRole("button", { name: "File it" }));

    await screen.findByText("task-146");
    expect((received as unknown as TaskCreateRequest).title).toBe("Filed without durability");
  });
});
