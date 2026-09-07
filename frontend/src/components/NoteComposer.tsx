import { useState } from "react";

import type { ReviewIdentity } from "../api/generated";
import { identityHeadline } from "./identityProblem";

/**
 * Write a note to the task's log, as yourself, from the page you are already on.
 *
 * This exists because the dispatch guard refuses a task whose newest log entry was not
 * written by a human, and until now the browser had no way to write one. A `ready` task
 * an agent filed reads "write the note that authorises this run first" and offered no
 * control that could — the remedy was reachable only from the CLI. Task-185.
 *
 * It is not a dispatch control and deliberately does not live inside the dispatch
 * panel. A note is the ordinary way a person says something on the record; that it also
 * satisfies the human-clocked rule is a consequence of the rule, not the reason the box
 * is here. Keeping it separate means it still renders on a project where dispatch was
 * never configured, and on a closed task somebody is annotating after the fact.
 *
 * Collapsed until asked for, because a permanently open textarea on every task page
 * pushes the record itself below the fold for the many readers who came to read.
 */
export type NoteComposerProps = {
  /** Who the server would attribute a write to, or why it will not attribute one. */
  identity: ReviewIdentity;
  busy?: boolean;
  error?: string | null;
  onAddNote: (body: string) => Promise<void> | void;
};

export function NoteComposer({ identity, busy = false, error = null, onAddNote }: NoteComposerProps) {
  const [open, setOpen] = useState(false);
  const [body, setBody] = useState("");

  // The same explanation as before, and for the same reason: a page that silently omits
  // a control is the defect task-185 closed.
  if (!identity.ok || !identity.user) {
    return (
      <p className="w-full rounded-lg border border-dark-border bg-dark-bg p-3 text-sm">
        <strong className="text-yellow-300">{identityHeadline(identity.problem)}</strong>
        <span className="text-dark-muted">{identity.detail}</span>
      </p>
    );
  }

  // Closed, this is one small button. It was a full-width card carrying a heading and
  // two lines of prose above a button, permanently, on a page whose reader is almost
  // always here to read the record rather than to write to it -- so the prose has moved
  // inside the form, where somebody is about to need it, and the card appears with it.
  //
  // Lit rather than flat for the same reason as the Fields opener beside it: an
  // icon-only control that looks quiet reads as decoration. The name is in `aria-label`
  // and `title`, so the tooltip, the screen reader and every test agree on it.
  if (!open) {
    return (
      <button
        type="button"
        disabled={busy}
        aria-label="Add a note"
        title="Add a note"
        onClick={() => {
          setOpen(true);
          setBody("");
        }}
        className="touch-target min-w-11 justify-center rounded-lg border border-blue-500/50 bg-blue-950/40 px-3 text-lg text-blue-300 hover:bg-blue-900/60 hover:text-blue-200 disabled:opacity-60"
      >
        <span aria-hidden="true">✚</span>
      </button>
    );
  }

  return (
    <section
      className="w-full space-y-3 rounded-xl border border-dark-border bg-dark-surface p-4 @min-[768px]:p-6"
      aria-label="Notes"
    >
      <h2 className="text-lg font-semibold">Notes</h2>
      <p className="text-sm text-dark-muted">
        Says something on the record without moving the task. A note is also what
        authorises a dispatch, since every run must trace to a human's entry.
      </p>

      <form
        className="space-y-3"
        onSubmit={(event) => {
          event.preventDefault();
          const value = body.trim();
          if (!value) return;
          // Cleared and closed only on success. A rejected save keeps the text in the
          // box; the error the page renders is beside it.
          void Promise.resolve(onAddNote(value)).then(
            () => {
              setBody("");
              setOpen(false);
            },
            () => undefined,
          );
        }}
      >
        <label htmlFor="task-note" className="block text-sm font-semibold">
          Note
        </label>
        <textarea
          id="task-note"
          required
          rows={4}
          value={body}
          onChange={(event) => setBody(event.target.value)}
          placeholder="What you want on the record…"
          className="w-full rounded-lg border border-dark-border bg-dark-bg p-3 text-dark-text focus:border-blue-500 focus:outline-none"
        />
        <p className="text-sm text-dark-muted">
          Written as <strong className="text-dark-text">{identity.user}</strong>.
        </p>

        {error && (
          <p role="alert" className="text-sm text-red-300">
            {error}
          </p>
        )}

        {/* Cancel sits beside Save rather than at the top of the card, and is grey
            rather than blue -- the same shape and the same colour as the Fields form's,
            because two forms on one page whose cancel is in a different place and a
            different colour is two things to learn instead of one. Grey also because
            the loud control should be the one that keeps the work, not the one that
            throws it away. */}
        <div className="mobile-action-row flex gap-3">
          <button
            type="submit"
            disabled={busy || !body.trim()}
            className="touch-target rounded-lg bg-blue-700 px-4 font-semibold text-white disabled:opacity-60"
          >
            {busy ? "Saving…" : "Save note"}
          </button>
          <button
            type="button"
            onClick={() => {
              setOpen(false);
              setBody("");
            }}
            className="touch-target rounded-lg px-4 font-semibold text-dark-muted hover:bg-dark-border"
          >
            Cancel
          </button>
        </div>
      </form>
    </section>
  );
}
