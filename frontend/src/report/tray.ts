import type { TaskCreateRequest } from "../api/generated";
import { toUploads, type PendingAttachment } from "./attachments";

/**
 * The collected-findings tray: many captures held locally, filed in one action.
 *
 * **This is composition state and nothing else.** A tray item is a request that has not
 * been sent yet, which is why the model below holds a `TaskCreateRequest` rather than
 * anything resembling an issue. There is no second store of findings anywhere: the
 * moment an item is accepted by the server it stops existing here and exists only as an
 * ordinary task, and everything that reads findings reads tasks.
 *
 * **The request is built when the finding is collected, not when the batch is sent.**
 * That is what makes each created task carry the page it was noticed on rather than the
 * page that happened to be behind the dialog fifteen findings later: `issueReport.ts`
 * bakes the route, the viewed task's `related` edge and the provenance prose into the
 * request at collect time, and this module never touches them again. It also means
 * every item goes through exactly the builder a single capture goes through, so the two
 * paths cannot drift in tags, attribution or provenance.
 *
 * **`operation_id` is minted once, at collect time, and stored.** The single-capture
 * path mints a fresh one per submit, which is right for it -- each press of File it is
 * a new intention. A tray item is the opposite: the same intention may be sent twice
 * because the first attempt's answer was lost, and the server resolves a repeat of a
 * known `operation_id` to the task the first attempt made. Removing confirmed
 * successes is the first line of defence against a duplicate; this is the one that
 * holds when the success was never seen.
 */

/** One finding, collected and not yet filed. */
export type TrayItem = {
  /** Local identity: the IndexedDB key, and what a removal names. */
  id: string;
  /**
   * Collection order, because the store is keyed by a UUID and returns nothing useful.
   *
   * A counter derived from the items already held rather than a clock, so the order is
   * reproducible in a test and two findings collected in the same millisecond cannot
   * tie.
   */
  order: number;
  /** The project this item files into, frozen when it was collected. */
  projectId: string;
  /** Exactly what will be POSTed, minus the images. Carries the stable operation_id. */
  request: TaskCreateRequest;
  /** Held apart from the request so the tray can draw a thumbnail without decoding it. */
  attachments: Array<PendingAttachment>;
  /** The route the finding was noticed on. Shown on the card; already in the request. */
  route: string;
};

/** What happened to one item in a batch. Session state: an error is not worth storing. */
export type TrayFiling = {
  itemId: string;
  title: string;
  projectId: string;
  /** The created task, when the server confirmed one. */
  taskId: string | null;
  /** The sentence to show on the card, when it did not. */
  error: string | null;
};

/** The next collection order, so a new item lands at the end of the list. */
export function nextOrder(items: ReadonlyArray<TrayItem>): number {
  return items.reduce((highest, item) => Math.max(highest, item.order), 0) + 1;
}

/** The tray in the order it was collected in. */
export function inCollectedOrder(items: ReadonlyArray<TrayItem>): Array<TrayItem> {
  return [...items].sort((left, right) => left.order - right.order);
}

/**
 * The request for one item, images and all.
 *
 * The images are joined back on here rather than stored inside the request, so the tray
 * holds one copy of the bytes and can render them.
 */
export function trayRequest(item: TrayItem): TaskCreateRequest {
  return { ...item.request, attachments: toUploads(item.attachments) };
}

/** The error to show on a card, from the last batch. Null once it is filed or retried. */
export function errorFor(itemId: string, batch: ReadonlyArray<TrayFiling>): string | null {
  return batch.find((filing) => filing.itemId === itemId)?.error ?? null;
}

/**
 * The label on the one button that submits the tray.
 *
 * Spelled out rather than "Create all", because the count is the thing worth being sure
 * about before pressing it.
 */
export function submitLabel(count: number): string {
  return count === 1 ? "Create 1 task" : `Create ${count} tasks`;
}

/**
 * What to say once a batch has finished, when some of it failed.
 *
 * Partial success stated as partial success. The alternative -- one "some items could
 * not be filed" line -- is what makes a person re-press the button to find out which,
 * and re-pressing is the thing that has to be safe rather than the thing to encourage.
 *
 * Takes one batch's outcomes, not the session's: a retry that sends the one item that
 * failed has to say "1 task created", and counting the successes of the batch before it
 * would say "5 of 6" about a button press that touched one thing.
 */
export function batchSummary(batch: ReadonlyArray<TrayFiling>): string {
  const won = batch.filter((filing) => filing.taskId !== null).length;
  const lost = batch.length - won;
  if (batch.length === 0) return "";
  if (lost === 0) return won === 1 ? "1 task created." : `${won} tasks created.`;
  if (won === 0)
    return lost === 1
      ? "Nothing was created. The finding is still here, with its error."
      : `Nothing was created. All ${lost} findings are still here, each with its error.`;
  return (
    `${won} of ${won + lost} created. The ${lost === 1 ? "one that failed is" : `${lost} that failed are`} ` +
    "still here with their text and images; fix and press the button again."
  );
}
