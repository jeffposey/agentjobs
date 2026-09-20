import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { client } from "../api/generated/client.gen";
import type { TaskCreateRequest } from "../api/types";
import type { TrayItem } from "../report/tray";
import { memoryTrayStore, setTrayStore, type TrayStore } from "../report/trayStore";
import { apiMockServer } from "../test/api-mock";
import { CaptureControl } from "./CaptureControl";

/**
 * Collecting a review's worth of findings and filing them in one action (task-121).
 *
 * These drive the real control through the real form, with the store swapped for an
 * in-memory one: jsdom has no IndexedDB, so the durability half is checked in
 * `e2e/capture-tray.spec.ts` where a reload is a real reload. What is checked here is
 * everything the store cannot tell you -- that a collect keeps the keyboard in the flow,
 * that a batch sends each card's own request, and that a partial failure leaves exactly
 * the cards that failed.
 */

let store: TrayStore;
/** Every create the app posted, in order, so a retry's arithmetic is checkable. */
let posted: Array<{ projectId: string; body: TaskCreateRequest }>;

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

/** A create endpoint that files everything, unless `refuse` names the title. */
function creating(refuse: (title: string) => string | null = () => null) {
  return [
    http.get("*/api/projects", () =>
      HttpResponse.json([
        project("agentjobs", "AgentJobs", "Jeff Posey"),
        project("mastercalls", "Mastercalls", "Jeff Posey"),
      ]),
    ),
    http.post("*/api/projects/:projectId/tasks", async ({ request, params }) => {
      const body = (await request.json()) as TaskCreateRequest;
      posted.push({ projectId: String(params.projectId), body });
      const refusal = refuse(body.title);
      if (refusal) {
        return HttpResponse.json({ code: "invalid_task", message: refusal }, { status: 400 });
      }
      return HttpResponse.json(
        { id: `task-50${posted.length}-filed`, title: body.title },
        { status: 201 },
      );
    }),
  ];
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
  fireEvent.click(screen.getByRole("button", { name: /^New task or issue/ }));
  await screen.findByRole("dialog", { name: "New task" });
  // The destination decides who is filing, so until the projects list arrives the form
  // correctly refuses to do anything.
  await waitFor(() =>
    expect(
      screen.getByRole("combobox", { name: "File into project" }).children.length,
    ).toBeGreaterThan(0),
  );
}

function title() {
  return screen.getByRole("textbox", { name: /^Title/ });
}

function fill(text: string, details: string) {
  fireEvent.change(title(), { target: { value: text } });
  fireEvent.change(screen.getByRole("textbox", { name: /^What happened/ }), {
    target: { value: details },
  });
}

/** Ctrl+Enter, the gesture the whole feature is shaped around. */
function collectWithKeyboard() {
  fireEvent.keyDown(title(), { key: "Enter", ctrlKey: true });
}

function tray() {
  return screen.getByRole("region", { name: "Collected findings" });
}

function pasteImage(target: HTMLElement, file: File) {
  fireEvent.paste(target, {
    clipboardData: { items: [{ kind: "file", getAsFile: () => file }], files: [] },
  });
}

/** A one-pixel PNG, as `readImage` will see it off the clipboard. */
function png(name: string) {
  const bytes = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
  return new File([bytes], name, { type: "image/png" });
}

/** A finding already on this device, as a reload finds it. */
function stored(id: string, order: number, overrides: Partial<TrayItem> = {}): TrayItem {
  return {
    id,
    order,
    projectId: "agentjobs",
    route: "/p/agentjobs/tasks",
    attachments: [],
    request: {
      title: `Finding ${id}`,
      description: `Something was wrong.\n\n---\nReported from the AgentJobs UI by Jeff Posey, at \`/p/agentjobs/tasks\`.`,
      lifecycle: "draft",
      tags: ["reported-issue"],
      actor: "Jeff Posey",
      operation_id: `0000000${order}-1111-4111-8111-111111111111`,
      attachments: [],
      dependencies: [],
    },
    ...overrides,
  };
}

beforeEach(() => {
  store = memoryTrayStore();
  setTrayStore(store);
  posted = [];
  client.setConfig({ baseUrl: "http://localhost" });
});

afterEach(() => setTrayStore(null));

describe("collecting findings", () => {
  it("adds a finding with the keyboard and leaves the cursor ready for the next one", async () => {
    apiMockServer.use(...creating());
    await openCapture("/p/agentjobs/tasks");

    fill("The filters match nothing", "Every filter returns zero rows.");
    collectWithKeyboard();

    // The dialog is still open, which is the whole point: a review pass does not want a
    // modal closing between findings.
    expect(screen.getByRole("dialog", { name: "New task" })).toBeInTheDocument();
    expect(within(await screen.findByRole("region", { name: "Collected findings" })).getByText(
      "The filters match nothing",
    )).toBeInTheDocument();
    // Emptied and focused, so the next finding is typed rather than clicked towards.
    await waitFor(() => expect(title()).toHaveValue(""));
    expect(title()).toHaveFocus();
    expect(screen.getByRole("textbox", { name: /^What happened/ })).toHaveValue("");

    fill("The log timestamps wrap", "Three lines each on a phone.");
    collectWithKeyboard();

    await waitFor(() =>
      expect(within(tray()).getAllByRole("listitem").map((row) => row.textContent)).toHaveLength(
        2,
      ),
    );
    expect(within(tray()).getByText("The log timestamps wrap")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create 2 tasks" })).toBeInTheDocument();
    // Nothing has been filed. A tray is unsent composition state and nothing else.
    expect(posted).toHaveLength(0);
  });

  it("leaves a plain Enter alone, so the form still submits the way every other one does", async () => {
    apiMockServer.use(...creating());
    await openCapture("/p/agentjobs/tasks");

    fill("Filed, not collected", "Enter on its own is a submit.");
    fireEvent.keyDown(title(), { key: "Enter" });

    expect(screen.queryByRole("region", { name: "Collected findings" })).not.toBeInTheDocument();
    expect(title()).toHaveValue("Filed, not collected");
  });

  it("refuses an empty finding without adding a blank card", async () => {
    apiMockServer.use(...creating());
    await openCapture("/p/agentjobs/tasks");

    fireEvent.change(title(), { target: { value: "A title with no note" } });
    collectWithKeyboard();

    expect(await screen.findByRole("alert")).toHaveTextContent("needs a title and a note");
    expect(screen.queryByRole("region", { name: "Collected findings" })).not.toBeInTheDocument();
    // And what was typed is still there to finish.
    expect(title()).toHaveValue("A title with no note");
  });

  it("keeps each finding's own pasted screenshots", async () => {
    apiMockServer.use(...creating());
    await openCapture("/p/agentjobs/tasks");

    fill("The badge is unreadable", "See the capture.");
    pasteImage(screen.getByRole("textbox", { name: /^What happened/ }), png("badge.png"));
    await screen.findByRole("list", { name: "Attached images" });
    fireEvent.click(screen.getByRole("button", { name: "Add to the list" }));

    const withImage = await screen.findByRole("list", {
      name: "Screenshots for The badge is unreadable",
    });
    expect(within(withImage).getByRole("img", { name: "badge.png" })).toBeInTheDocument();
    expect(within(tray()).getByText(/1 screenshot/)).toBeInTheDocument();

    // The next finding starts clean rather than inheriting the first one's image.
    fill("The empty state is blank", "No prose at all.");
    fireEvent.click(screen.getByRole("button", { name: "Add to the list" }));
    await waitFor(() => expect(within(tray()).getAllByText(/captured at/)).toHaveLength(2));
    expect(
      screen.queryByRole("list", { name: "Screenshots for The empty state is blank" }),
    ).not.toBeInTheDocument();
  });

  it("writes each finding to the device's store as it is collected", async () => {
    apiMockServer.use(...creating());
    await openCapture("/p/agentjobs/tasks");

    fill("Stored on collect", "So a reload does not erase a review session.");
    collectWithKeyboard();

    await waitFor(async () => expect(await store.load()).toHaveLength(1));
    const [held] = await store.load();
    expect(held?.request.title).toBe("Stored on collect");
    expect(held?.route).toBe("/p/agentjobs/tasks");
  });

  it("counts what is waiting on the trigger itself, before the dialog is opened", async () => {
    // A tray nobody can tell they still have is the same leak as not persisting one,
    // arriving a step later. This is what a reload shows, so it is read off the store.
    apiMockServer.use(...creating());
    await store.put(stored("a", 1));
    await store.put(stored("b", 2));
    renderControl("/p/agentjobs/tasks");

    const trigger = await screen.findByRole("button", {
      name: "New task or issue (2 collected)",
    });
    expect(within(trigger.parentElement!).getByText("2")).toBeInTheDocument();
  });

  it("brings back what this device already held, screenshots and all", async () => {
    apiMockServer.use(...creating());
    await store.put(
      stored("a", 1, {
        attachments: [
          {
            id: "shot",
            label: "badge.png",
            mediaType: "image/png",
            sizeBytes: 8,
            dataBase64: "iVBORw0K",
            preview: "data:image/png;base64,iVBORw0K",
          },
        ],
      }),
    );
    await openCapture("/p/agentjobs/tasks");

    const restored = await screen.findByRole("region", { name: "Collected findings" });
    expect(within(restored).getByText("Finding a")).toBeInTheDocument();
    expect(within(restored).getByRole("img", { name: "badge.png" })).toHaveAttribute(
      "src",
      "data:image/png;base64,iVBORw0K",
    );
  });

  it("removes a card the person changed their mind about, here and on the device", async () => {
    apiMockServer.use(...creating());
    await store.put(stored("a", 1));
    await store.put(stored("b", 2));
    await openCapture("/p/agentjobs/tasks");
    await screen.findByText("Finding a");

    fireEvent.click(within(tray()).getAllByRole("button", { name: "Remove" })[0]!);

    await waitFor(() => expect(screen.queryByText("Finding a")).not.toBeInTheDocument());
    expect((await store.load()).map((entry) => entry.id)).toEqual(["b"]);
  });
});

describe("filing the tray", () => {
  it("creates every card in one action and links each task it made", async () => {
    apiMockServer.use(...creating());
    await store.put(stored("a", 1));
    await store.put(stored("b", 2));
    await openCapture("/p/agentjobs/tasks");
    await screen.findByText("Finding a");

    fireEvent.click(screen.getByRole("button", { name: "Create 2 tasks" }));

    const filed = await screen.findByRole("region", { name: "Tasks created" });
    expect(within(filed).getByRole("link", { name: "task-501-filed" })).toBeInTheDocument();
    expect(within(filed).getByRole("link", { name: "task-502-filed" })).toBeInTheDocument();
    // Everything confirmed has left the tray, and the device's store with it.
    await waitFor(() =>
      expect(screen.queryByRole("region", { name: "Collected findings" })).not.toBeInTheDocument(),
    );
    expect(await store.load()).toEqual([]);
    expect(posted).toHaveLength(2);
  });

  it("sends each card through the ordinary create path, carrying what it was collected with", async () => {
    apiMockServer.use(...creating());
    // Two findings noticed in different projects on different pages -- the case the
    // acceptance criteria are about, since a batch is filed from wherever the person
    // happens to be standing at the end.
    await store.put(stored("a", 1));
    await store.put(
      stored("b", 2, {
        projectId: "mastercalls",
        route: "/p/mastercalls/dashboard",
        request: {
          ...stored("b", 2).request,
          title: "Finding b",
          description: "Noticed on the dashboard.\n\n---\nat `/p/mastercalls/dashboard`.",
        },
      }),
    );
    await openCapture("/p/agentjobs/tasks");
    await screen.findByText("Finding a");

    fireEvent.click(screen.getByRole("button", { name: "Create 2 tasks" }));
    await screen.findByRole("region", { name: "Tasks created" });

    // Each one goes to its own project, with its own page in its own description, rather
    // than to the project and page that were on screen when the button was pressed.
    expect(posted.map((call) => call.projectId)).toEqual(["agentjobs", "mastercalls"]);
    expect(posted[0]?.body.description).toContain("/p/agentjobs/tasks");
    expect(posted[1]?.body.description).toContain("/p/mastercalls/dashboard");
    // And they are ordinary reported-issue creates: the same tag, the same attribution,
    // the same lifecycle a single capture produces.
    for (const call of posted) {
      expect(call.body.tags).toEqual(["reported-issue"]);
      expect(call.body.actor).toBe("Jeff Posey");
      expect(call.body.lifecycle).toBe("draft");
    }
  });

  it("keeps a failed card, says why on the card, and files the rest", async () => {
    apiMockServer.use(
      ...creating((title) =>
        title === "Finding b" ? "'nope' is not a category in this project." : null,
      ),
    );
    await store.put(stored("a", 1));
    await store.put(stored("b", 2));
    await store.put(stored("c", 3));
    await openCapture("/p/agentjobs/tasks");
    await screen.findByText("Finding a");

    fireEvent.click(screen.getByRole("button", { name: "Create 3 tasks" }));

    // The one that failed is still on screen, with its error attached to it rather than
    // summarised above it.
    const remaining = await screen.findByRole("region", { name: "Collected findings" });
    await waitFor(() => expect(within(remaining).getAllByText(/captured at/)).toHaveLength(1));
    expect(within(remaining).getByText("Finding b")).toBeInTheDocument();
    expect(within(remaining).getByRole("alert")).toHaveTextContent("is not a category");
    // Stated as a partial success, with the count.
    expect(screen.getByText(/2 of 3 created/)).toBeInTheDocument();
    // And the device's store holds exactly the one that did not land.
    expect((await store.load()).map((entry) => entry.id)).toEqual(["b"]);
  });

  it("retries only what failed, with the operation_id that item was collected with", async () => {
    // Two things have to hold for a retry to be safe: nothing confirmed is resent, and
    // what is resent is recognisable to the server as the same intention. The first is
    // the count below; the second is the id.
    let refuseOnce = true;
    apiMockServer.use(
      ...creating((title) => {
        if (title === "Finding b" && refuseOnce) return "The store was locked.";
        return null;
      }),
    );
    await store.put(stored("a", 1));
    await store.put(stored("b", 2));
    await openCapture("/p/agentjobs/tasks");
    await screen.findByText("Finding a");

    fireEvent.click(screen.getByRole("button", { name: "Create 2 tasks" }));
    await screen.findByText(/1 of 2 created/);
    expect(posted).toHaveLength(2);

    refuseOnce = false;
    fireEvent.click(screen.getByRole("button", { name: "Create 1 task" }));

    await waitFor(() =>
      expect(screen.queryByRole("region", { name: "Collected findings" })).not.toBeInTheDocument(),
    );
    // Three creates in total, not four: the one that landed was never sent again.
    expect(posted).toHaveLength(3);
    expect(posted[2]?.body.title).toBe("Finding b");
    // The same operation_id both times, so a success whose answer was lost resolves to
    // the task the first attempt made instead of a second one.
    expect(posted[2]?.body.operation_id).toBe(posted[1]?.body.operation_id);
    // Both attempts' successes are on screen; the retry did not discard the first batch's.
    const filed = screen.getByRole("region", { name: "Tasks created" });
    expect(within(filed).getAllByRole("listitem")).toHaveLength(2);
  });

  it("says nothing was created when the whole batch failed, and keeps every card", async () => {
    apiMockServer.use(...creating(() => "The server is not reachable."));
    await store.put(stored("a", 1));
    await store.put(stored("b", 2));
    await openCapture("/p/agentjobs/tasks");
    await screen.findByText("Finding a");

    fireEvent.click(screen.getByRole("button", { name: "Create 2 tasks" }));

    expect(await screen.findByText(/Nothing was created/)).toBeInTheDocument();
    expect(within(tray()).getAllByRole("alert")).toHaveLength(2);
    expect((await store.load()).map((entry) => entry.id)).toEqual(["a", "b"]);
    expect(screen.queryByRole("region", { name: "Tasks created" })).not.toBeInTheDocument();
  });

  it("files one finding now without disturbing the tray it is sitting beside", async () => {
    // The two paths coexist: a finding worth filing on its own still is, and doing so
    // must not take the collected list with it.
    apiMockServer.use(...creating());
    await store.put(stored("a", 1));
    await openCapture("/p/agentjobs/tasks");
    await screen.findByText("Finding a");

    fill("Filed on its own", "Worth filing without the batch.");
    fireEvent.click(screen.getByRole("button", { name: "File it" }));

    await screen.findByText(/Filed as/);
    expect(within(tray()).getByText("Finding a")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create 1 task" })).toBeInTheDocument();
    expect((await store.load()).map((entry) => entry.id)).toEqual(["a"]);
  });
});
