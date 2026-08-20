import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { ReviewIdentity } from "../api/generated";
import { NoteComposer } from "./NoteComposer";

/**
 * The control that closes task-185's dead end: a dispatch refusal told a human to write
 * an authorising entry, and no surface in the browser could write one.
 *
 * These assert on the words a person reads and the value their click sends, never on a
 * `data-` attribute. A test that found the section existed would pass just as happily
 * while the button did nothing, which is the shape of the defect this closes.
 */

const identified: ReviewIdentity = { ok: true, user: "Jeff Posey", problem: null, detail: "" };

function renderComposer(
  props: Partial<Parameters<typeof NoteComposer>[0]> = {},
  onAddNote = vi.fn(async (_body: string) => undefined),
) {
  render(<NoteComposer identity={identified} onAddNote={onAddNote} {...props} />);
  return onAddNote;
}

describe("writing a note from the task page", () => {
  it("offers a control named exactly as the dispatch refusal names it", () => {
    renderComposer();

    expect(screen.getByRole("button", { name: /add a note/i })).toBeInTheDocument();
  });

  it("sends what was typed, and says whose entry it will be", async () => {
    const onAddNote = renderComposer();

    fireEvent.click(screen.getByRole("button", { name: /add a note/i }));
    expect(screen.getByText(/written as/i)).toHaveTextContent("Jeff Posey");

    fireEvent.change(screen.getByLabelText("Note"), {
      target: { value: "Authorising this run." },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save note" }));

    await waitFor(() => expect(onAddNote).toHaveBeenCalledWith("Authorising this run."));
    // Closed once saved, so the record it was written to is what the reader sees next.
    await waitFor(() => expect(screen.queryByLabelText("Note")).toBeNull());
  });

  it("will not send an empty note", () => {
    const onAddNote = renderComposer();

    fireEvent.click(screen.getByRole("button", { name: /add a note/i }));
    fireEvent.change(screen.getByLabelText("Note"), { target: { value: "   " } });
    fireEvent.click(screen.getByRole("button", { name: "Save note" }));

    expect(onAddNote).not.toHaveBeenCalled();
  });

  it("keeps the text and shows the reason when the save is refused", async () => {
    const onAddNote = vi.fn(async () => {
      throw new Error("refused");
    });
    render(
      <NoteComposer identity={identified} error="Unknown actor 'Jeff Posey'." onAddNote={onAddNote} />,
    );

    fireEvent.click(screen.getByRole("button", { name: /add a note/i }));
    fireEvent.change(screen.getByLabelText("Note"), { target: { value: "Please keep me." } });
    fireEvent.click(screen.getByRole("button", { name: "Save note" }));

    await waitFor(() => expect(onAddNote).toHaveBeenCalled());
    expect(screen.getByLabelText("Note")).toHaveValue("Please keep me.");
    expect(screen.getByRole("alert")).toHaveTextContent("Unknown actor 'Jeff Posey'.");
  });

  it("explains why there is no control when the project configures nobody to write as", () => {
    renderComposer({
      identity: {
        ok: false,
        user: null,
        problem: "none",
        detail: "Add a human to 'actors:' in .agentjobs/config.yaml.",
      },
    });

    expect(screen.queryByRole("button", { name: /add a note/i })).toBeNull();
    expect(screen.getByText(/no user configured/i)).toBeInTheDocument();
    expect(
      screen.getByText("Add a human to 'actors:' in .agentjobs/config.yaml."),
    ).toBeInTheDocument();
  });
});
