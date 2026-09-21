/**
 * The one IndexedDB database behind everything a capture holds on this device.
 *
 * **One database, one connection, one version.** Task-121 opened `agentjobs-capture`
 * for the collected tray; task-512 added the draft of the finding currently being
 * typed. Two modules calling `indexedDB.open` for themselves is not a style question:
 * a connection held open at version 1 makes an open at version 2 throw `VersionError`
 * and blocks the upgrade for as long as the first one lives, so the second store would
 * simply never appear. The version and the store names therefore live here, and both
 * callers share the connection this module opens.
 *
 * **Adding a store is a version bump and nothing else.** `onupgradeneeded` creates any
 * store that is missing, so a device carrying a version 1 tray gains the draft store on
 * its next page load without losing what is in the tray.
 */

const DB_NAME = "agentjobs-capture";
/** 1: the tray (task-121). 2: the open capture form's draft (task-512). */
const DB_VERSION = 2;

export const TRAY_STORE = "tray";
export const DRAFT_STORE = "draft";

const STORES = [TRAY_STORE, DRAFT_STORE];

/** One IDB request as a promise, so a store reads like ordinary asynchronous code. */
export function promised<T>(request: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () =>
      reject(request.error ?? new Error("The capture store refused."));
  });
}

function open(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, DB_VERSION);
    request.onupgradeneeded = () => {
      for (const name of STORES) {
        if (!request.result.objectStoreNames.contains(name)) {
          request.result.createObjectStore(name, { keyPath: "id" });
        }
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () =>
      reject(request.error ?? new Error("The capture store would not open."));
    // A second tab holding an older version open blocks the upgrade forever otherwise.
    request.onblocked = () =>
      reject(new Error("Another tab is holding the capture store open."));
  });
}

// Opened lazily and shared. Reopening per call would serialise every capture behind a
// handshake, and these stores are written to on every keystroke-sized act.
let connection: Promise<IDBDatabase> | null = null;

/** The shared connection, opened on first use. */
export function captureDb(): Promise<IDBDatabase> {
  connection ??= open();
  return connection;
}

/** Run one readwrite transaction against `store`, resolving when it has committed. */
export async function writeTo(
  store: string,
  apply: (objectStore: IDBObjectStore) => void,
): Promise<void> {
  const transaction = (await captureDb()).transaction(store, "readwrite");
  const done = new Promise<void>((resolve, reject) => {
    transaction.oncomplete = () => resolve();
    transaction.onerror = () =>
      reject(transaction.error ?? new Error("The write failed."));
    transaction.onabort = () =>
      reject(transaction.error ?? new Error("The write aborted."));
  });
  apply(transaction.objectStore(store));
  await done;
}

/** Read from `store` inside one readonly transaction. */
export async function readFrom<T>(
  store: string,
  ask: (objectStore: IDBObjectStore) => IDBRequest<T>,
): Promise<T> {
  const transaction = (await captureDb()).transaction(store, "readonly");
  return promised(ask(transaction.objectStore(store)));
}
