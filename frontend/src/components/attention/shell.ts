import type { AttentionNotification } from "./episode";

/**
 * The three things AgentJobs asks the Windows shell for, each behind a seam (task-422).
 *
 * All of them are optional at runtime and none of them may take the page down. The
 * badge does not exist outside an installed app, the favicon is cosmetic, and a
 * notification can be refused by a permission, by the browser, or silently by Windows
 * Focus Assist. So every function here answers with what happened rather than throwing,
 * and the caller records nothing that depends on a pixel having been drawn.
 */

/** Above this the badge reads as a dot with no number, which is Chrome's own rule. */
export const BADGE_MAX = 99;

type BadgingNavigator = {
  setAppBadge?: (count?: number) => Promise<void>;
  clearAppBadge?: () => Promise<void>;
};

/**
 * Put the count on the taskbar icon, or take it off.
 *
 * `navigator.setAppBadge` is the supported Windows mechanism for an installed PWA:
 * Chrome draws the badge as an overlay on the app's taskbar icon, and Windows keeps
 * drawing it while the app is closed. In an ordinary browser tab the call resolves and
 * does nothing, which is the right shape -- the page does not have to know whether it
 * is installed.
 *
 * **What this does not control is the badge's colour**, which is the browser's. That is
 * why {@link attentionFaviconHref} exists beside it rather than instead of it: the icon
 * AgentJobs *does* control goes unmistakably red, so the red treatment is ours in the
 * tab strip and in the window, whatever Chrome paints on the taskbar.
 */
export async function applyAppBadge(
  count: number,
  navigatorLike?: BadgingNavigator,
): Promise<"set" | "cleared" | "unsupported"> {
  const target = navigatorLike ?? (typeof navigator === "undefined" ? undefined : navigator);
  if (!target) return "unsupported";
  try {
    if (count > 0) {
      if (!target.setAppBadge) return "unsupported";
      await target.setAppBadge(Math.min(count, BADGE_MAX));
      return "set";
    }
    if (!target.clearAppBadge) return "unsupported";
    await target.clearAppBadge();
    return "cleared";
  } catch {
    // A browser that exposes the method and refuses the call -- an uninstalled PWA on
    // some platforms does this -- is not a failure worth reporting anywhere.
    return "unsupported";
  }
}

export const QUIET_FAVICON = "/app/icons/icon-192.png";

/**
 * A red disc carrying the count, as a data URL.
 *
 * SVG rather than a canvas so it is a pure function of the number: it can be asserted
 * as a string, it needs no DOM to build, and it stays crisp at whatever size the
 * browser asks for. `#ef4444` is the same red as the in-app badge (`bg-red-500`), so
 * the tab, the header and the dashboard alarm are one colour rather than three.
 */
export function attentionFaviconHref(count: number): string {
  const label = count > 9 ? "9+" : String(count);
  const size = count > 9 ? 30 : 38;
  const svg = [
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">`,
    `<circle cx="32" cy="32" r="32" fill="#ef4444"/>`,
    `<text x="32" y="33" font-family="Segoe UI, Arial, sans-serif" font-size="${size}"`,
    ` font-weight="700" fill="#ffffff" text-anchor="middle" dominant-baseline="central">`,
    label,
    `</text></svg>`,
  ].join("");
  return `data:image/svg+xml,${encodeURIComponent(svg)}`;
}

/**
 * Make the document's icon say whether anything is waiting.
 *
 * The link element is created on first use: `index.html` declares a manifest and an
 * Apple touch icon but no `rel="icon"`, so before this there was nothing to swap and
 * the tab showed the browser's blank page mark.
 *
 * Cosmetic by construction -- a document that will not let us touch its head simply
 * keeps the icon it had.
 */
export function paintFavicon(count: number, doc?: Document): void {
  const target = doc ?? (typeof document === "undefined" ? undefined : document);
  if (!target?.head) return;
  let link = target.querySelector<HTMLLinkElement>('link[rel="icon"]');
  if (!link) {
    link = target.createElement("link");
    link.rel = "icon";
    target.head.appendChild(link);
  }
  const next = count > 0 ? attentionFaviconHref(count) : QUIET_FAVICON;
  link.type = count > 0 ? "image/svg+xml" : "image/png";
  if (link.getAttribute("href") !== next) link.setAttribute("href", next);
}

export type DeliveryOutcome = "shown" | "blocked" | "unsupported" | "failed";

type NotificationConstructorLike = {
  permission: NotificationPermission;
  new (title: string, options?: NotificationOptions): Notification;
};

type ServiceWorkerLike = {
  getRegistration?: (scope?: string) => Promise<ServiceWorkerRegistration | undefined>;
};

/**
 * Raise the bottom-right Windows notification, and say what became of the attempt.
 *
 * **The service worker first, and a page notification only as a fallback.** A
 * notification owned by the page dies with the page, and the case this feature exists
 * for is a window that is minimised, behind something, or closed. The worker's
 * notification outlives all of those, and its `notificationclick` handler can focus an
 * existing window or open a new one -- see `service-worker.js`.
 *
 * **`shown` is not "the person saw it".** Windows Focus Assist and Do Not Disturb
 * suppress the banner without telling the page, so a suppressed notification returns
 * `shown` here and the episode stays unacknowledged, which is the behaviour that makes
 * quiet hours degrade correctly: the interruption is lost, the red indicator is not,
 * and the badge is what greets the person when they come back.
 */
export async function deliver(
  note: AttentionNotification,
  deps?: {
    notification?: NotificationConstructorLike;
    serviceWorker?: ServiceWorkerLike;
  },
): Promise<DeliveryOutcome> {
  const Ctor =
    deps?.notification ??
    ((typeof window !== "undefined" && "Notification" in window
      ? window.Notification
      : undefined) as NotificationConstructorLike | undefined);
  if (!Ctor) return "unsupported";
  if (Ctor.permission !== "granted") return "blocked";

  const options: NotificationOptions = {
    body: note.body,
    tag: note.tag,
    // Never `true`: a repeat of the same tag is an *update* of a number, and renotify
    // would make every task joining an open episode buzz again, which is the fatigue
    // the episode model exists to prevent.
    renotify: false,
    data: { url: note.url, episodeId: note.episodeId },
    icon: QUIET_FAVICON,
    badge: QUIET_FAVICON,
  } as NotificationOptions;

  const container =
    deps?.serviceWorker ??
    (typeof navigator !== "undefined" && "serviceWorker" in navigator
      ? (navigator.serviceWorker as unknown as ServiceWorkerLike)
      : undefined);

  try {
    const registration = await container?.getRegistration?.("/app/");
    if (registration?.showNotification) {
      await registration.showNotification(note.title, options);
      return "shown";
    }
    new Ctor(note.title, options);
    return "shown";
  } catch {
    return "failed";
  }
}
