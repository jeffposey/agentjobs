import { TRAY_STORE, readFrom, writeTo } from "./captureDb";
import type { PendingAttachment } from "./attachments";
import type { TrayItem } from "./tray";

/**
 * Where the unsent tray lives between one page load and the next.
 *
 * **IndexedDB, because the tray holds screenshots.** `localStorage` is a synchronous
 * string store with a few megabytes to its name, and one pasted screenshot is up to
 * five; a review pass holding a dozen would exceed it and throw in the middle of a
 * capture. IndexedDB is asynchronous, has no comparable ceiling, and is the mechanism
 * the task specifies.
 *
 * **One record per item, keyed by the item's id.** Rewriting the whole list on every
 * change would mean rewriting every screenshot's bytes to add a title, and removing one
 * confirmed success would mean rewriting the rest. A keyed store makes a collect a
 * single `put` and a confirmed success a single `delete`, which is exactly the
 * granularity the partial-failure rule needs.
 *
 * **The preview data URL is not stored.** It is `data:<type>;base64,<bytes>` and the
 * bytes are already in the record beside it, so storing both would double a tray of
 * screenshots on disk for nothing. {@link hydrate} rebuilds it on the way out.
 *
 * **It can be absent, and the tray still works.** A private window, blocked site data
 * or a browser that refuses to open the database leaves `durable` false: the tray is
 * then session state, which the UI says out loud rather than implying a durability it
 * does not have.
 *
 * The database itself is opened by `captureDb.ts`, which the draft of the finding being
 * typed shares (task-512). Two modules opening `agentjobs-capture` at two versions is
 * how the second store never gets created.
 */

/** A tray item as it sits in the database: no derived preview. */
type StoredAttachment = Omit<PendingAttachment, "preview">;
type StoredItem = Omit<TrayItem, "attachments"> & { attachments: Array<StoredAttachment> };

/** Drop what can be recomputed, so one copy of each screenshot's bytes is stored. */
export function dehydrate(item: TrayItem): StoredItem {
  return {
    ...item,
    attachments: item.attachments.map(({ preview: _preview, ...rest }) => rest),
  };
}

/** Rebuild the thumbnail's data URL from the bytes that were stored. */
export function hydrate(stored: StoredItem): TrayItem {
  return {
    ...stored,
    attachments: stored.attachments.map((attachment) => ({
      ...attachment,
      preview: `data:${attachment.mediaType};base64,${attachment.dataBase64}`,
    })),
  };
}

export type TrayStore = {
  /** Everything held for this device, in collection order. Never rejects. */
  load: () => Promise<Array<TrayItem>>;
  /** Write one item. Never rejects: a failed write costs durability, not the capture. */
  put: (item: TrayItem) => Promise<void>;
  /** Forget items the server has confirmed, or the person has removed. */
  remove: (ids: ReadonlyArray<string>) => Promise<void>;
  /** False when this browser will not keep the tray across a reload. */
  durable: boolean;
};

/** The real store, against this browser's IndexedDB. */
export function indexedDbTrayStore(): TrayStore {
  return {
    durable: true,
    load: async () => {
      try {
        const stored = await readFrom<Array<StoredItem>>(TRAY_STORE, (store) =>
          store.getAll(),
        );
        return stored.map(hydrate).sort((left, right) => left.order - right.order);
      } catch {
        // An unreadable store is an empty tray, not a page that will not render. The
        // alternative is a capture control that throws on a browser with site data
        // blocked, which is worse than losing a list nobody has filed yet.
        return [];
      }
    },
    put: async (item) => {
      try {
        await writeTo(TRAY_STORE, (store) => store.put(dehydrate(item)));
      } catch {
        /* Durability lost, capture kept. The caller holds it in React state regardless. */
      }
    },
    remove: async (ids) => {
      try {
        await writeTo(TRAY_STORE, (store) => {
          for (const id of ids) store.delete(id);
        });
      } catch {
        /* A stale record is re-loaded next time and can be removed again by hand. */
      }
    },
  };
}

/** The store for a browser with no IndexedDB, and the one tests inject. */
export function memoryTrayStore(seed: ReadonlyArray<TrayItem> = []): TrayStore {
  const held = new Map(seed.map((item) => [item.id, item]));
  return {
    durable: false,
    load: () =>
      Promise.resolve([...held.values()].sort((left, right) => left.order - right.order)),
    put: (item) => {
      held.set(item.id, item);
      return Promise.resolve();
    },
    remove: (ids) => {
      for (const id of ids) held.delete(id);
      return Promise.resolve();
    },
  };
}

let shared: TrayStore | null = null;

/**
 * The one tray store this tab uses.
 *
 * Shared rather than made per component, because the capture control is mounted in two
 * places -- the header, and beside the routes for pages with no header -- and two
 * connections writing the same records would be two sources of truth for one list.
 */
export function trayStore(): TrayStore {
  shared ??= typeof indexedDB === "undefined" ? memoryTrayStore() : indexedDbTrayStore();
  return shared;
}

/** Point the app at a different store. For tests; nothing in the app calls it. */
export function setTrayStore(store: TrayStore | null): void {
  shared = store;
}
