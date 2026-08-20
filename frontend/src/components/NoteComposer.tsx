import { useState } from "react";

import type { ReviewIdentity } from "../api/generated";

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

  return (
    <section
      className="space-y-3 rounded-xl border border-dark-border bg-dark-surface p-4 min-[820px]:p-6"
      aria-label="Notes"
    >
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold">Notes</h2>
          <p className="mt-1 text-sm text-dark-muted">
            Says something on the record without moving the task. A note is also what
            authorises a dispatch, since every run must trace to a human's entry.
          </p>
        </div>
        {identity.ok && identity.user && (
          <button
            type="button"
            disabled={busy}
            onClick={() => {
              setOpen((current) => !current);
              setBody("");
            }}
            className="touch-target rounded-lg bg-blue-700 px-4 font-semibold text-white hover:bg-blue-600 disabled:opacity-60"
          >
            {open ? "Cancel" : "✎ Add a note"}
          </button>
        )}
      </div>

      {!identity.ok || !identity.user ? (
        // The same explanation the review panel gives, for the same reason: a page that
        // silently omits the control named by a refusal is the defect this closes.
        <div className="rounded-lg border border-dark-border bg-dark-bg p-4 text-sm">
          <strong className="text-yellow-300">
            {identity.problem === "multiple" ? "Multiple users configured. " : "No user configured. "}
          </strong>
          <span className="text-dark-muted">{identity.detail}</span>
        </div>
      ) : (
        open && (
          <form
            className="space-y-3"
            onSubmit={(event) => {
              event.preventDefault();
              const value = body.trim();
              if (!value) return;
              // Cleared and closed only on success. A rejected save keeps the text
              // in the box; the error the page renders is beside it.
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
            <div className="mobile-action-row flex gap-3">
              <button
                type="submit"
                disabled={busy || !body.trim()}
                className="touch-target rounded-lg bg-blue-700 px-4 font-semibold text-white disabled:opacity-60"
              >
                Save note
              </button>
            </div>
          </form>
        )
      )}

      {error && (
        <p role="alert" className="text-sm text-red-300">
          {error}
        </p>
      )}
    </section>
  );
}
