import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { ReviewIdentity } from "../api/generated";
import type { TaskRead } from "../api/types";
import { TaskFields, fieldsChangedBetween, type TaskFieldsPatch } from "./TaskFields";

/**
 * Editing a task's authoring fields from the browser (task-230).
 *
 * These assert on the words a person reads and on the patch their click sends, never on
 * the presence of a control. The interesting properties are all about what is *not*
 * sent: an untouched field, a field typed back to its original value, and — the one
 * that would be a real defect — anything resembling a workflow move.
 *
 * The concurrency and attribution halves of this feature are not asserted here and
 * cannot be: jsdom has no server to refuse a stale revision and no log to attribute an
 * edit to. `frontend/e2e/edit-fields.spec.ts` covers both against a running one.
 */

const identified: ReviewIdentity = { ok: true, user: "Jeff Posey", problem: null, detail: "" };

function task(overrides: Partial<TaskRead> = {}): TaskRead {
  return {
    schema: 2,
    id: "task-401",
    title: "Groom the backlog",
    created: "2026-09-01T00:00:00Z",
    updated: "2026-09-01T00:00:00Z",
    lifecycle: "ready",
    ball: "agent",
    ball_reason: "available",
    ball_prompt: null,
    outcome: null,
    archived: false,
    priority: "medium",
    category: "feature",
    effort: "half a day",
    tags: ["gui", "grooming"],
    spec: { summary: "A summary.", description: "A description." },
    acceptance: [],
    deliverables: [],
    dependencies: [],
    links: [],
    branches: [],
    log: [],
    display_status: "Ready",
    ...overrides,
  } as unknown as TaskRead;
}

function renderFields(
  props: Partial<Parameters<typeof TaskFields>[0]> = {},
  onSave = vi.fn(async (_patch: TaskFieldsPatch) => undefined),
) {
  const view = render(<TaskFields task={task()} identity={identified} onSave={onSave} {...props} />);
  return { onSave, view };
}

function openEditor() {
  fireEvent.click(screen.getByRole("button", { name: /Edit fields/ }));
}

describe("editing a task's fields", () => {
  it("sends only the field that was changed", async () => {
    const { onSave } = renderFields();

    openEditor();
    fireEvent.change(screen.getByLabelText("Priority"), { target: { value: "critical" } });
    fireEvent.click(screen.getByRole("button", { name: "Save fields" }));

    await waitFor(() => expect(onSave).toHaveBeenCalledWith({ priority: "critical" }));
  });

  it("will not send an edit that changes nothing", () => {
    const { onSave } = renderFields();

    openEditor();
    // Typed away and typed back. The control is dirty; the record is not.
    fireEvent.change(screen.getByLabelText("Title"), { target: { value: "Something else" } });
    fireEvent.change(screen.getByLabelText("Title"), { target: { value: "Groom the backlog" } });

    expect(screen.getByRole("button", { name: "Save fields" })).toBeDisabled();
    expect(onSave).not.toHaveBeenCalled();
  });

  it("refuses to save a task with its title emptied, and says why", () => {
    const { onSave } = renderFields();

    openEditor();
    fireEvent.change(screen.getByLabelText("Title"), { target: { value: "   " } });

    expect(screen.getByRole("alert")).toHaveTextContent("A task needs a title");
    expect(screen.getByRole("button", { name: "Save fields" })).toBeDisabled();
    expect(onSave).not.toHaveBeenCalled();
  });

  it("adds and removes tags, and sends the whole collection", async () => {
    const { onSave } = renderFields();

    openEditor();
    fireEvent.click(screen.getByRole("button", { name: "Remove tag gui" }));
    fireEvent.change(screen.getByLabelText("Add a tag"), { target: { value: "api, backend" } });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    fireEvent.click(screen.getByRole("button", { name: "Save fields" }));

    // A whole-collection replace, which is what the patch route accepts: the removal and
    // both additions arrive as one list rather than as three operations.
    await waitFor(() =>
      expect(onSave).toHaveBeenCalledWith({ tags: ["grooming", "api", "backend"] }),
    );
  });

  it("adds a tag on Enter without submitting the form", () => {
    const { onSave } = renderFields();

    openEditor();
    const box = screen.getByLabelText("Add a tag");
    fireEvent.change(box, { target: { value: "phone" } });
    fireEvent.keyDown(box, { key: "Enter" });

    expect(screen.getByRole("button", { name: "Remove tag phone" })).toBeInTheDocument();
    // The keypress finished a word, not the edit.
    expect(onSave).not.toHaveBeenCalled();
  });

  it("does not send anything the workflow verbs own", async () => {
    const { onSave } = renderFields();

    openEditor();
    // There is no control for these at all, which is the point: a dropdown writing
    // `lifecycle: closed` would be the one mutation in this system with no reason
    // recorded against it.
    expect(screen.queryByLabelText("Status")).toBeNull();
    expect(screen.queryByLabelText("Lifecycle")).toBeNull();
    expect(screen.queryByLabelText("Ball")).toBeNull();

    fireEvent.change(screen.getByLabelText("Effort"), { target: { value: "an afternoon" } });
    fireEvent.click(screen.getByRole("button", { name: "Save fields" }));

    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    const patch = onSave.mock.calls[0]![0] as Record<string, unknown>;
    for (const owned of ["lifecycle", "ball", "ball_reason", "ball_prompt", "outcome"]) {
      expect(patch).not.toHaveProperty(owned);
    }
  });

  it("keeps the edits in the boxes when the save is refused", async () => {
    const onSave = vi.fn(async () => {
      throw new Error("refused");
    });
    renderFields({ error: "This task changed while you had it open, so nothing was saved." }, onSave);

    openEditor();
    fireEvent.change(screen.getByLabelText("Title"), { target: { value: "A better title" } });
    fireEvent.click(screen.getByRole("button", { name: "Save fields" }));

    await waitFor(() => expect(onSave).toHaveBeenCalled());
    expect(screen.getByLabelText("Title")).toHaveValue("A better title");
  });

  it("closes once the save lands, so the reader is looking at the record", async () => {
    const { onSave } = renderFields();

    openEditor();
    fireEvent.change(screen.getByLabelText("Category"), { target: { value: "chore" } });
    fireEvent.click(screen.getByRole("button", { name: "Save fields" }));

    await waitFor(() => expect(onSave).toHaveBeenCalledWith({ category: "chore" }));
    await waitFor(() => expect(screen.queryByLabelText("Category")).toBeNull());
  });

  it("fetches the completion vocabulary only once somebody opens the form", () => {
    const onOpen = vi.fn();
    renderFields({ onOpen });

    expect(onOpen).not.toHaveBeenCalled();
    openEditor();
    expect(onOpen).toHaveBeenCalledTimes(1);
  });

  it("offers the tags this project already uses, minus the ones already on the task", () => {
    renderFields({ vocabulary: { tags: ["api", "gui", "grooming"], categories: ["feature"] } });

    openEditor();
    const offered = [...document.querySelectorAll("#task-field-tags option")].map(
      (option) => (option as HTMLOptionElement).value,
    );
    // "gui" and "grooming" are on the task already; offering them would be an
    // invitation to add a duplicate.
    expect(offered).toEqual(["api"]);
  });

  it("explains itself instead of offering a control when nothing can be attributed", () => {
    renderFields({
      identity: { ok: false, user: null, problem: "no_actors", detail: "This project configures no human actor." },
    });

    expect(screen.queryByRole("button", { name: /Edit fields/ })).toBeNull();
    expect(screen.getByText(/configures no human actor/)).toBeInTheDocument();
  });
});

describe("naming what moved underneath an edit", () => {
  it("names the fields that changed between two reads", () => {
    const before = task();
    const after = task({ priority: "critical", tags: ["gui"], updated: "2026-09-02T00:00:00Z" });

    expect(fieldsChangedBetween(before, after)).toEqual([
      "priority went from medium to critical",
      "tags are now gui",
    ]);
  });

  it("says nothing changed when the record moved for another reason", () => {
    // A note appended to the log bumps `updated` and touches nothing this form edits.
    expect(fieldsChangedBetween(task(), task({ updated: "2026-09-02T00:00:00Z" }))).toEqual([]);
  });

  it("puts what moved beside the refusal, so the conflict names a field and not a timestamp", async () => {
    const onSave = vi.fn(async () => {
      throw new Error("refused");
    });
    const { view } = renderFields({}, onSave);

    openEditor();
    fireEvent.change(screen.getByLabelText("Effort"), { target: { value: "an afternoon" } });
    fireEvent.click(screen.getByRole("button", { name: "Save fields" }));
    await waitFor(() => expect(onSave).toHaveBeenCalled());

    // The parent re-read the record and re-rendered with the newer one, exactly as the
    // conflict path in App.tsx does.
    view.rerender(
      <TaskFields
        task={task({ priority: "critical", updated: "2026-09-02T00:00:00Z" })}
        identity={identified}
        error="This task changed while you had it open, so nothing was saved."
        onSave={onSave}
      />,
    );

    expect(screen.getByRole("alert")).toHaveTextContent("priority went from medium to critical");
    // And the edit survived the round trip.
    expect(screen.getByLabelText("Effort")).toHaveValue("an afternoon");
  });

  it("follows the record for a field the person never touched", () => {
    const { view } = renderFields();

    openEditor();
    fireEvent.change(screen.getByLabelText("Effort"), { target: { value: "an afternoon" } });
    view.rerender(
      <TaskFields
        task={task({ priority: "low", updated: "2026-09-02T00:00:00Z" })}
        identity={identified}
        onSave={vi.fn(async () => undefined)}
      />,
    );

    // Somebody else lowered the priority. The form shows their value rather than
    // holding the one it opened with and quietly putting it back on the next save.
    expect(screen.getByLabelText("Priority")).toHaveValue("low");
    expect(screen.getByLabelText("Effort")).toHaveValue("an afternoon");
  });
});
