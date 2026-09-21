import type { Priority } from "../api/generated";
import type { PendingAttachment } from "./attachments";
import { DRAFT_STORE, readFrom, writeTo } from "./captureDb";
import { REPORTED_ISSUE_TAG } from "./issueReport";

/**
 * The finding currently being typed, kept where the page cannot take it (task-512).
 *
 * **The tray was durable and the form was not**, which is the asymmetry this exists to
 * remove: a review pass that collected twelve findings got all twelve back after a
 * reload and lost the thirteenth, the one being written at the moment the page went
 * away. Since a frontend rebuild can reload the tab with nothing asked, "the moment the
 * page went away" is not a rare accident.
 *
 * **The same database as the tray, for the same reason.** A draft can hold a pasted
 * screenshot, so `localStorage` is out on its size ceiling alone -- the argument is
 * written out in `trayStore.ts` and is not repeated here. `captureDb.ts` owns the
 * connection both stores share.
 *
 * **A draft is composition state with a short life.** It exists between one keystroke
 * and the moment the finding becomes something else: a tray item, or a task. The form
 * clears it itself at that moment rather than leaving it to be reconciled later, which
 * is what keeps a restored draft from being able to duplicate a finding that was
 * already collected or filed.
 *
 * **Keyed, because more than one form can be open over a device's lifetime.** The
 * capture dialog's draft is `capture`; a surface that wants its own uses its own key,
 * and nothing is shared between them by accident.
 */

/**
 * Everything typed into a capture form, in a shape that survives `structuredClone`.
 *
 * Deliberately not a `TaskCreateRequest`: a request is what a *finished* finding
 * becomes, built once at collect or submit time by `issueReport.ts`, and a half-typed
 * form cannot produce a valid one. So this is the form's own state, restored back into
 * the same controls the person was looking at.
 *
 * `startOnFile` is absent on purpose. `DispatchOnCreate.tsx` has the argument: a
 * remembered "spend money on this" is the default that feature exists not to have, and
 * it is unchecked on every mount for that reason. A draft that restored it would be a
 * back door to exactly the thing.
 */
export type CaptureDraft = {
  title: string;
  details: string;
  attachments: Array<PendingAttachment>;
  /** The project selected in the picker, or "" while the form is still on its default. */
  destination: string;
  actionable: boolean;
  priority: Priority;
  /** Whether the specification section was open, so it comes back open if it held text. */
  expanded: boolean;
  wantsDraft: boolean;
  /**
   * The specification inputs, by their `name`.
   *
   * Those fields are uncontrolled -- read through `FormData` at submit -- which is what
   * lets a model's draft write into them and an undo restore the person's own text. So
   * they are collected by name here rather than mirrored into React state, which would
   * be a second source of truth for what is in the box.
   */
  fields: Record<string, string>;
};

/** What a pristine form already contains, so its own defaults do not read as content. */
export const DEFAULT_FIELDS: Readonly<Record<string, string>> = {
  category: "general",
  tags: REPORTED_ISSUE_TAG,
};

/** A draft as it sits in the database: no derived preview, and its key. */
type StoredAttachment = Omit<PendingAttachment, "preview">;
type StoredDraft = Omit<CaptureDraft, "attachments"> & {
  id: string;
  attachments: Array<StoredAttachment>;
};

/** Drop what can be recomputed, so one copy of each screenshot's bytes is stored. */
export function dehydrate(key: string, draft: CaptureDraft): StoredDraft {
  return {
    ...draft,
    id: key,
    attachments: draft.attachments.map(
      ({ preview: _preview, ...rest }) => rest,
    ),
  };
}

/** Rebuild the thumbnails' data URLs from the bytes that were stored. */
export function hydrate(stored: StoredDraft): CaptureDraft {
  const { id: _id, ...draft } = stored;
  return {
    ...draft,
    attachments: draft.attachments.map((attachment) => ({
      ...attachment,
      preview: `data:${attachment.mediaType};base64,${attachment.dataBase64}`,
    })),
  };
}

/**
 * Whether this draft is worth keeping, and whether the page is holding unsent work.
 *
 * One predicate for both questions on purpose: "there is something here to lose" is
 * exactly what decides whether to write a record *and* whether a service worker may
 * reload the tab. Two definitions of that would drift, and the pair that matters --
 * stored but reloadable, or reload-blocking but not stored -- are both wrong.
 *
 * A form's own prefilled values are not content. Someone who has opened the dialog and
 * typed nothing has nothing to lose, and a draft written for them would restore a form
 * identical to the empty one.
 */
export function draftHasContent(draft: CaptureDraft): boolean {
  if (
    draft.title.trim() ||
    draft.details.trim() ||
    draft.attachments.length > 0
  )
    return true;
  return Object.entries(draft.fields).some(
    ([name, value]) => value.trim() !== (DEFAULT_FIELDS[name] ?? ""),
  );
}

export type DraftStore = {
  /** What was being typed on this device, or null. Never rejects. */
  load: (key: string) => Promise<CaptureDraft | null>;
  /** Keep it. Never rejects: a failed write costs durability, not the capture. */
  save: (key: string, draft: CaptureDraft) => Promise<void>;
  /** Forget it, because it became a tray item or a task, or was emptied. */
  clear: (key: string) => Promise<void>;
  /** False when this browser will not keep a draft across a reload. */
  durable: boolean;
};

/** The real store, against this browser's IndexedDB. */
export function indexedDbDraftStore(): DraftStore {
  return {
    durable: true,
    load: async (key) => {
      try {
        const stored = await readFrom<StoredDraft | undefined>(
          DRAFT_STORE,
          (store) => store.get(key),
        );
        return stored ? hydrate(stored) : null;
      } catch {
        // An unreadable store is no draft, not a page that will not render -- the same
        // rule the tray follows, and for the same reason.
        return null;
      }
    },
    save: async (key, draft) => {
      try {
        await writeTo(DRAFT_STORE, (store) => store.put(dehydrate(key, draft)));
      } catch {
        /* Durability lost, composition kept. The form holds it in React state regardless. */
      }
    },
    clear: async (key) => {
      try {
        await writeTo(DRAFT_STORE, (store) => store.delete(key));
      } catch {
        /* A stale draft is offered again next time and can be emptied by hand. */
      }
    },
  };
}

/** The store for a browser with no IndexedDB, and the one tests inject. */
export function memoryDraftStore(
  seed: Record<string, CaptureDraft> = {},
): DraftStore {
  const held = new Map(Object.entries(seed));
  return {
    durable: false,
    load: (key) => Promise.resolve(held.get(key) ?? null),
    save: (key, draft) => {
      held.set(key, draft);
      return Promise.resolve();
    },
    clear: (key) => {
      held.delete(key);
      return Promise.resolve();
    },
  };
}

let shared: DraftStore | null = null;

/**
 * The one draft store this tab uses.
 *
 * Shared rather than made per component, for the reason `trayStore` is: the capture
 * control is mounted in two places and only one is on screen at a time, so two
 * connections writing the same record would be two opinions about one draft.
 */
export function draftStore(): DraftStore {
  shared ??=
    typeof indexedDB === "undefined"
      ? memoryDraftStore()
      : indexedDbDraftStore();
  return shared;
}

/** Point the app at a different store. For tests; nothing in the app calls it. */
export function setDraftStore(store: DraftStore | null): void {
  shared = store;
}
