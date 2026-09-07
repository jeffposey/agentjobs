import { useState } from "react";

import type { Priority, ReviewIdentity } from "../api/generated";
import type { TaskRead } from "../api/types";
import { identityHeadline } from "./identityProblem";

/**
 * Change a task's authoring fields from the browser: priority, tags, effort, category,
 * title (task-230).
 *
 * The backend has accepted these since schema v2. What was missing was reach: a task
 * could be created in the browser and acted on in the browser, and between those two
 * points every field was read-only, so raising a priority or fixing a tag meant an
 * agent session or a hand-edited record. Grooming a backlog is mostly those two acts,
 * and they happen in the fifteen minutes somebody has on a phone or not at all.
 *
 * **There is no status control here, and that is a design decision rather than an
 * omission.** `lifecycle`, `ball`, `ball_reason` and `outcome` move only through
 * `claim`, `handoff`, `release` and `close`, each of which appends its own entry saying
 * who moved it and why. A dropdown writing `lifecycle: closed` would be the one
 * mutation in this system with no recorded reason, and the argument for a task record
 * over a chat message is precisely that the record says why. The patch schema leaves
 * those fields out for the same reason, so the control could not exist even if somebody
 * wanted it.
 *
 * **An edit is not a workflow move.** The patch carries no ball, so editing a task
 * parked at human/review leaves it parked at human/review — the panel above this one
 * does not move, blink or disappear when a priority changes.
 */

/** The fields this surface can change, as a patch: absent means "leave it alone". */
export type TaskFieldsPatch = {
  title?: string;
  priority?: Priority;
  category?: string;
  effort?: string;
  tags?: Array<string>;
};

export type TaskFieldsProps = {
  /** The record as last read. Every displayed value not being edited comes from here. */
  task: TaskRead;
  /** Who the server would attribute the edit to, or why it will not attribute one. */
  identity: ReviewIdentity;
  /**
   * Tags and categories already in use in this project, offered as completion.
   *
   * A tag nobody else spells the same way is not a tag, so the vocabulary is the point
   * of the control rather than a nicety. It is optional because it costs a request:
   * see {@link TaskFieldsProps.onOpen}.
   */
  vocabulary?: { tags: Array<string>; categories: Array<string> };
  /**
   * Called the first time the form is opened, so the caller can fetch the vocabulary
   * then rather than on every page load. The completion lists are worth a request from
   * somebody who is editing and worth nothing to the far larger number of people who
   * opened this page to read it.
   */
  onOpen?: () => void;
  busy?: boolean;
  error?: string | null;
  /** Resolves when the patch lands; rejects when it does not, leaving the form open. */
  onSave: (patch: TaskFieldsPatch) => Promise<void> | void;
};

const PRIORITIES: Array<Priority> = ["critical", "high", "medium", "low"];

const inputClass =
  "touch-target mt-1 w-full rounded-lg border border-dark-border bg-dark-bg px-3 py-2 text-dark-text placeholder:text-dark-muted focus:border-blue-500 focus:outline-none";

/** What the record currently says, in the shape the form edits. */
function current(task: TaskRead): Required<TaskFieldsPatch> {
  return {
    title: task.title,
    priority: (task.priority ?? "medium") as Priority,
    category: task.category ?? "",
    effort: task.effort ?? "",
    tags: task.tags ?? [],
  };
}

function sameTags(left: Array<string>, right: Array<string>) {
  return left.length === right.length && left.every((tag, index) => tag === right[index]);
}

/**
 * Which of the editable fields moved between two reads, in words.
 *
 * This is what makes a refused save readable. The server's own refusal names two
 * timestamps — true, and useless to somebody holding a phone: the question they have is
 * "what did I nearly overwrite", and a microsecond-resolution `updated` does not answer
 * it. Comparing the record the form opened against the record that came back names the
 * change instead.
 *
 * An empty list is a real answer and not a failure: a note appended to the log moves
 * `updated` without touching a single field this form edits, and saying so is what
 * tells the person their edit is still safe to make.
 */
export function fieldsChangedBetween(before: TaskRead, after: TaskRead): Array<string> {
  const was = current(before);
  const now = current(after);
  const changes: Array<string> = [];
  if (was.title !== now.title) changes.push(`the title is now "${now.title}"`);
  if (was.priority !== now.priority) {
    changes.push(`priority went from ${was.priority} to ${now.priority}`);
  }
  if (was.category !== now.category) {
    changes.push(`category went from ${was.category || "none"} to ${now.category || "none"}`);
  }
  if (was.effort !== now.effort) {
    changes.push(`effort went from ${was.effort || "none"} to ${now.effort || "none"}`);
  }
  if (!sameTags(was.tags, now.tags)) {
    changes.push(`tags are now ${now.tags.length ? now.tags.join(", ") : "empty"}`);
  }
  return changes;
}

export function TaskFields({
  task,
  identity,
  vocabulary,
  onOpen,
  busy = false,
  error = null,
  onSave,
}: TaskFieldsProps) {
  const [open, setOpen] = useState(false);
  // The record this form was opened against. Kept so a refused save can say what moved
  // underneath it, which the server's own message cannot.
  const [baseline, setBaseline] = useState<TaskRead | null>(null);
  // Only the fields the person has actually touched. Everything else is read from the
  // record on every render, so a field somebody else changed while this form was open
  // follows the record rather than being silently reverted by a save that includes it.
  const [draft, setDraft] = useState<TaskFieldsPatch>({});
  const [tagDraft, setTagDraft] = useState("");

  const live = current(task);
  const value = { ...live, ...draft };

  // Only what differs from the record right now. A field typed back to its original
  // value is not an edit, and sending it would put a field nobody changed into the
  // log entry naming what this edit touched.
  const patch: TaskFieldsPatch = {};
  if (draft.title !== undefined && draft.title.trim() !== live.title) patch.title = draft.title.trim();
  if (draft.priority !== undefined && draft.priority !== live.priority) patch.priority = draft.priority;
  if (draft.category !== undefined && draft.category.trim() !== live.category) {
    patch.category = draft.category.trim();
  }
  if (draft.effort !== undefined && draft.effort.trim() !== live.effort) patch.effort = draft.effort.trim();
  if (draft.tags !== undefined && !sameTags(draft.tags, live.tags)) patch.tags = draft.tags;
  const dirty = Object.keys(patch).length > 0;
  // A title is the one field that cannot be emptied: the record has to be nameable.
  const titleEmpty = value.title.trim().length === 0;

  const moved = baseline && baseline.updated !== task.updated ? fieldsChangedBetween(baseline, task) : null;

  const reset = () => {
    setOpen(false);
    setBaseline(null);
    setDraft({});
    setTagDraft("");
  };

  const addTag = (raw: string) => {
    // Commas split, because "gui, frontend" is what a person types and refusing it
    // teaches them nothing.
    const additions = raw
      .split(",")
      .map((tag) => tag.trim())
      .filter(Boolean);
    if (!additions.length) return;
    const next = [...value.tags];
    for (const tag of additions) if (!next.includes(tag)) next.push(tag);
    setDraft((state) => ({ ...state, tags: next }));
    setTagDraft("");
  };

  const removeTag = (tag: string) => {
    setDraft((state) => ({ ...state, tags: value.tags.filter((entry) => entry !== tag) }));
  };

  const editable = identity.ok && Boolean(identity.user);

  // A page that silently omits a control is a page whose reader concludes the feature
  // is gone -- task-185's lesson, and why this explains itself rather than rendering
  // nothing.
  if (!editable) {
    return (
      <p className="w-full rounded-lg border border-dark-border bg-dark-bg p-3 text-sm">
        <strong className="text-yellow-300">{identityHeadline(identity.problem)}</strong>
        <span className="text-dark-muted">{identity.detail}</span>
      </p>
    );
  }

  // Closed, this is one small button and nothing else -- no card, no heading, no
  // paragraph. It was a full-width bordered card whose whole content was a heading and
  // this button, sitting above the review panel: about a hundred and fifty pixels of a
  // phone screen spent saying "there is a control here", on every visit, to the large
  // majority of readers who came to read. The card earns its space once the form is in
  // it and not before.
  //
  // Lit rather than flat, because an icon-only control that looks quiet reads as
  // decoration: it carries the accent colour and a border instead of the muted grey the
  // rest of the strip uses. The name lives in `aria-label` and `title` -- what a screen
  // reader announces, what the tooltip says, and what every test asks for it by. The
  // glyph is decoration and is marked as such.
  if (!open) {
    return (
      <button
        type="button"
        disabled={busy}
        aria-label="Edit fields"
        title="Edit fields"
        onClick={() => {
          setBaseline(task);
          setOpen(true);
          onOpen?.();
        }}
        className="touch-target min-w-11 justify-center rounded-lg border border-blue-500/50 bg-blue-950/40 px-3 text-lg text-blue-300 hover:bg-blue-900/60 hover:text-blue-200 disabled:opacity-60"
      >
        <span aria-hidden="true">✎</span>
      </button>
    );
  }

  return (
    <section
      className="w-full space-y-3 rounded-xl border border-dark-border bg-dark-surface p-4 @min-[768px]:p-6"
      aria-label="Task fields"
    >
      <h2 className="text-lg font-semibold">Fields</h2>
      <form
        className="space-y-4"
        onSubmit={(event) => {
          event.preventDefault();
          if (!dirty || titleEmpty) return;
          // Closed only once the write lands. A refused save keeps every edit in
          // the boxes, because the banner beside them says what to do about it and
          // throwing a phone user's typing away is not a way to report a conflict.
          void Promise.resolve(onSave(patch)).then(reset, () => undefined);
        }}
      >
        <p className="text-sm text-dark-muted">
          Editing these does not move the task — who acts next changes through the
          actions elsewhere on this page, never here.
        </p>

        {/* One column on a phone, two where there is room. Every control is a
            `touch-target`, so the whole form is thumb-sized at 390px. */}
        <div className="grid gap-4 @min-[768px]:grid-cols-2">
          <label className="block text-sm font-semibold @min-[768px]:col-span-2">
            Title
            <input
              name="title"
              value={value.title}
              onChange={(event) => setDraft((state) => ({ ...state, title: event.target.value }))}
              className={inputClass}
            />
          </label>

          <label className="block text-sm font-semibold">
            Priority
            <select
              name="priority"
              value={value.priority}
              onChange={(event) =>
                setDraft((state) => ({ ...state, priority: event.target.value as Priority }))
              }
              className={inputClass}
            >
              {PRIORITIES.map((option) => (
                <option value={option} key={option}>
                  {option.charAt(0).toUpperCase() + option.slice(1)}
                </option>
              ))}
            </select>
          </label>

          <label className="block text-sm font-semibold">
            Category
            <input
              name="category"
              list="task-field-categories"
              value={value.category}
              onChange={(event) => setDraft((state) => ({ ...state, category: event.target.value }))}
              className={inputClass}
            />
          </label>
          <datalist id="task-field-categories">
            {vocabulary?.categories.map((entry) => <option value={entry} key={entry} />)}
          </datalist>

          <label className="block text-sm font-semibold @min-[768px]:col-span-2">
            Effort
            <input
              name="effort"
              value={value.effort}
              placeholder="Half a day"
              onChange={(event) => setDraft((state) => ({ ...state, effort: event.target.value }))}
              className={inputClass}
            />
          </label>
        </div>

        <div className="space-y-2">
          <span className="block text-sm font-semibold" id="task-field-tags-label">
            Tags
          </span>
          <ul className="flex flex-wrap gap-2" aria-labelledby="task-field-tags-label">
            {value.tags.map((tag) => (
              <li key={tag}>
                <button
                  type="button"
                  onClick={() => removeTag(tag)}
                  aria-label={`Remove tag ${tag}`}
                  className="touch-target rounded-full border border-dark-border bg-dark-bg px-3 text-sm hover:border-red-500 hover:text-red-300"
                >
                  {tag} <span aria-hidden="true">✕</span>
                </button>
              </li>
            ))}
            {value.tags.length === 0 && <li className="text-sm text-dark-muted">No tags yet.</li>}
          </ul>
          <div className="flex flex-wrap gap-2">
            <label className="sr-only" htmlFor="task-field-add-tag">
              Add a tag
            </label>
            <input
              id="task-field-add-tag"
              list="task-field-tags"
              value={tagDraft}
              placeholder="Add a tag"
              onChange={(event) => setTagDraft(event.target.value)}
              onKeyDown={(event) => {
                // Enter adds the tag rather than submitting the form: on a phone
                // the keyboard's return key is the obvious way to finish a word,
                // and having it save the whole edit instead would be a trap.
                if (event.key !== "Enter") return;
                event.preventDefault();
                addTag(tagDraft);
              }}
              className="touch-target min-w-40 flex-1 rounded-lg border border-dark-border bg-dark-bg px-3 py-2 text-dark-text placeholder:text-dark-muted focus:border-blue-500 focus:outline-none"
            />
            <datalist id="task-field-tags">
              {vocabulary?.tags
                .filter((tag) => !value.tags.includes(tag))
                .map((tag) => <option value={tag} key={tag} />)}
            </datalist>
            <button
              type="button"
              disabled={!tagDraft.trim()}
              onClick={() => addTag(tagDraft)}
              className="touch-target rounded-lg border border-dark-border bg-dark-bg px-4 text-sm font-semibold hover:bg-dark-border disabled:opacity-60"
            >
              Add
            </button>
          </div>
        </div>

        <p className="text-sm text-dark-muted">
          Saved as <strong className="text-dark-text">{identity.user}</strong>.
        </p>

        {titleEmpty && (
          <p role="alert" className="text-sm text-red-300">
            A task needs a title. Put one back before saving.
          </p>
        )}

        {error && (
          <div role="alert" className="space-y-1 rounded-lg border border-red-500/60 bg-red-950/40 p-3 text-sm text-red-200">
            <p>{error}</p>
            {/* What actually moved, rather than the two timestamps the server
                compared. An empty list is still worth saying: it means nothing this
                form edits was touched, so saving again is safe. */}
            {moved && (
              <p>
                {moved.length
                  ? `While you were editing: ${moved.join("; ")}.`
                  : "Nothing this form edits was changed — the record moved for another reason, such as a new log entry."}{" "}
                Your edits are still in the boxes above. Save again to apply them.
              </p>
            )}
          </div>
        )}

        <div className="mobile-action-row flex gap-3">
          <button
            type="submit"
            disabled={busy || !dirty || titleEmpty}
            className="touch-target rounded-lg bg-blue-700 px-4 font-semibold text-white disabled:opacity-60"
          >
            {busy ? "Saving…" : "Save fields"}
          </button>
          <button
            type="button"
            onClick={reset}
            className="touch-target rounded-lg px-4 font-semibold text-dark-muted hover:bg-dark-border"
          >
            Cancel
          </button>
        </div>
      </form>
    </section>
  );
}
