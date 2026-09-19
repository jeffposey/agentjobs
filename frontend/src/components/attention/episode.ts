import type { AttentionResponse } from "../../api/types";

/**
 * What a client does with an attention episode, as functions of data (task-422).
 *
 * Everything here is pure. The rule about *when* to interrupt lives on the server and
 * arrives as an episode id and an `acknowledged` flag; this module decides only what
 * this particular browser has already drawn, and what the notification should say. The
 * split matters because the answers are tested in opposite places: the policy against
 * a set of task ids in Python, the wording and the destination against a string here,
 * and neither needs a browser to be running.
 *
 * The one piece of state a client owns is the id of the last episode it notified for.
 * That is deliberately *not* on the server: two browsers open on the same project have
 * each drawn their own notification or not, and the server's episode is the same one
 * for both. See {@link readLastNotified}.
 */

export const NOTIFICATION_TAG_PREFIX = "agentjobs-attention-";
/**
 * One tag per project, so the OS *replaces* rather than stacks.
 *
 * Windows keeps delivered notifications in the Action Center. Without a tag, a second
 * client -- a second window, a reopened tab -- leaves two entries saying different
 * numbers, and the older one is a lie that persists until somebody clears it by hand.
 */
export function notificationTag(projectId: string): string {
  return `${NOTIFICATION_TAG_PREFIX}${projectId}`;
}

export const NOTIFIED_STORAGE_KEY = "agentjobs.attention.notified";
/** Query parameter carrying the episode a click is acknowledging. */
export const ACK_PARAM = "attention_ack";

export type AttentionNotification = {
  episodeId: string;
  title: string;
  body: string;
  tag: string;
  url: string;
};

/**
 * Where a person should land from the notification.
 *
 * One waiting task goes straight to it; several go to the filtered list, which is the
 * same `status=human` view the Dashboard's alarm links to when it has more than three.
 * Not the Dashboard itself: the notification already said the number, and the thing it
 * is for is getting to the work.
 *
 * The episode rides along as {@link ACK_PARAM} because activating the notification is
 * one of the three acts that acknowledge an episode, and the click may arrive at a tab
 * that does not exist yet.
 */
export function waitingPath(
  projectId: string,
  episode: { id: string; tasks: string[]; lead_task_id?: string | null },
): string {
  const base = `/app/p/${encodeURIComponent(projectId)}`;
  const ack = `${ACK_PARAM}=${encodeURIComponent(episode.id)}`;
  if (episode.tasks.length === 1 && episode.lead_task_id) {
    return `${base}/tasks/${encodeURIComponent(episode.lead_task_id)}?${ack}`;
  }
  return `${base}/tasks?status=human&${ack}`;
}

/**
 * The notification for an episode, or `null` when there is nothing to say.
 *
 * It summarises and names; it does not review. The ask, the branch and the evidence
 * stay in AgentJobs -- a toast is a wake-up signal, and a person who has read the whole
 * handoff in a bubble that disappears has read it in the one place they cannot act on.
 */
export function notificationFor(
  attention: AttentionResponse | null | undefined,
  projectId: string,
): AttentionNotification | null {
  const episode = attention?.episode;
  if (!episode || !attention) return null;

  const count = attention.blocking;
  if (count <= 0) return null;

  const lead = episode.lead_task_id
    ? `${episode.lead_task_id}: ${episode.lead_task_title ?? ""}`.trim()
    : "";
  const title = count === 1 ? "1 task is waiting on you" : `${count} tasks are waiting on you`;
  let body = lead || "Open AgentJobs to see what has stopped.";
  if (count > 1 && lead) {
    const others = count - 1;
    body = `${lead} — and ${others} other${others === 1 ? "" : "s"}.`;
  }

  return {
    episodeId: episode.id,
    title,
    body,
    tag: notificationTag(projectId),
    // The server's `deep_link` first (task-423). The rule now has three callers in two
    // languages -- this notifier, the push payload, and the service worker rendering a
    // push that arrived while no page was running -- so it is computed once, on the
    // server, and `waitingPath` stays as the fallback for a bundle talking to a server
    // that predates the field.
    url:
      episode.deep_link ||
      waitingPath(projectId, {
        id: episode.id,
        tasks: episode.tasks,
        lead_task_id: episode.lead_task_id,
      }),
  };
}

/**
 * Whether this client owes a notification right now.
 *
 * Three conditions, and each rules out a real way of being wrong: an acknowledged
 * episode is one the person has already acted on; an episode this client has already
 * drawn is the reason a reload is not an alert; and no episode at all is the quiet
 * state.
 *
 * Note what is **not** here: nothing asks whether the window is focused, hidden or
 * frontmost. A notification while you are looking at another app is the entire point,
 * and the browser-pane hazard in this repository's notes -- a tab that reports itself
 * permanently hidden -- would make any such test wrong in both directions.
 */
export function shouldNotify(
  attention: AttentionResponse | null | undefined,
  lastNotifiedId: string | null,
): boolean {
  const episode = attention?.episode;
  if (!episode) return false;
  if (episode.acknowledged) return false;
  return episode.id !== lastNotifiedId;
}

type StorageLike = Pick<Storage, "getItem" | "setItem" | "removeItem">;

/**
 * The last episode this browser drew a notification for.
 *
 * `localStorage` because it has to survive the tab closing -- the whole cold-start
 * case -- and because it is per-origin, which is per AgentJobs server, which is the
 * right scope. Every access is wrapped: a private window, blocked site data, or a
 * browser that throws on the *getter* would otherwise take the notifier down with it,
 * and the failure mode of losing this value is one extra notification.
 */
export function readLastNotified(storage?: StorageLike | null): string | null {
  try {
    const store = storage ?? window.localStorage;
    return store.getItem(NOTIFIED_STORAGE_KEY);
  } catch {
    return null;
  }
}

export function writeLastNotified(episodeId: string, storage?: StorageLike | null): void {
  try {
    const store = storage ?? window.localStorage;
    store.setItem(NOTIFIED_STORAGE_KEY, episodeId);
  } catch {
    // Nothing to do and nothing to report. The cost is a repeated notification for
    // this episode on the next cold start, which is noise rather than a defect.
  }
}

export type DeliveryState =
  | "granted"
  | "askable"
  | "denied"
  | "unsupported";

/**
 * Whether this browser can raise a Windows notification, and whose decision that is.
 *
 * `unsupported` and `denied` are different answers to the person even though both mean
 * no toast: one is a browser that cannot, the other is a permission they can change.
 * The spec asks for a clear degraded state, and "notifications are off" without saying
 * which of those it is leaves them with nothing to do about it.
 */
export function deliveryState(
  notification: { permission: NotificationPermission } | undefined,
): DeliveryState {
  if (!notification) return "unsupported";
  if (notification.permission === "granted") return "granted";
  if (notification.permission === "denied") return "denied";
  return "askable";
}
