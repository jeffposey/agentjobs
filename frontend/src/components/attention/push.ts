/**
 * What a browser has to know to receive a push, as functions of data (task-423).
 *
 * The same split as `episode.ts`: the rule about *when* to interrupt lives on the
 * server, and this module decides only what this particular device can do about it.
 * Everything here that can be pure is pure, because the interesting cases -- an iPhone
 * in a Safari tab, a browser with no `PushManager`, a permission the person has already
 * refused -- are exactly the ones that cannot be reproduced in a test environment.
 */

export const PUSH_CONTEXT_CACHE = "agentjobs-push-context";
export const PUSH_CONTEXT_KEY = "/__agentjobs_push_context__";
/**
 * Where the page leaves what the service worker needs to re-subscribe on its own.
 *
 * `pushsubscriptionchange` fires at a worker, possibly with no page open for weeks, and
 * the project id is not derivable there. The Cache API rather than IndexedDB because it
 * is two calls and the payload is forty bytes. These two constants are duplicated in
 * `service-worker.js` -- which is plain JavaScript shipped whole and cannot import
 * this -- and `push.test.ts` imports its source to assert they still agree.
 */

export const DEVICE_STORAGE_KEY = "agentjobs.push.device";
/** The id of the device row this browser registered, for its own Remove button. */

export const DETAIL_COUNT = "count";
export const DETAIL_TASK = "task";

export const PRIVACY_STORAGE_KEY = "agentjobs.push.privacy";

/**
 * Whether this device wants the quiet form of a push. **Off by default** (task-421).
 *
 * The server's `detail` on the subscription row is what delivery reads; this is the
 * same answer kept where the question is asked, so the toggle can render before any
 * round trip and so the preference survives a device being unregistered and registered
 * again. The two are written together: turning the toggle re-posts the subscription,
 * which is how a row changes `detail` without becoming a second row.
 *
 * Per device rather than per account, because that is the shape of the question. A
 * tablet on a desk at home and a phone held up on a train are the same person with
 * different bystanders.
 */
export function readPrivacy(storage?: StorageLike | null): boolean {
  try {
    return (storage ?? window.localStorage).getItem(PRIVACY_STORAGE_KEY) === "on";
  } catch {
    return false;
  }
}

export function writePrivacy(on: boolean, storage?: StorageLike | null): void {
  try {
    (storage ?? window.localStorage).setItem(PRIVACY_STORAGE_KEY, on ? "on" : "off");
  } catch {
    // The cost is a toggle that forgets, not a push that leaks: the row on the server
    // keeps whatever was last posted, and delivery reads that.
  }
}

/** The subscription's `detail` for a device whose privacy toggle is *on* or *off*. */
export function detailFor(privacy: boolean): string {
  return privacy ? DETAIL_COUNT : DETAIL_TASK;
}

export type PushAvailability =
  | "available"
  | "blocked"
  | "install-required"
  | "unsupported";

export type PushEnvironment = {
  hasServiceWorker: boolean;
  hasPushManager: boolean;
  hasNotification: boolean;
  permission: NotificationPermission | null;
  isApplePlatform: boolean;
  isStandalone: boolean;
  /**
   * A phone or tablet rather than a desktop.
   *
   * Which of the two notification panels applies, and nothing else. It is deliberately
   * not a capability: push and local notifications both work on both kinds of device,
   * so this decides which *answer* a person is offered, not which one the browser could
   * technically perform.
   */
  isHandheld: boolean;
};

/**
 * What this device can do, in the four words the panel has different sentences for.
 *
 * `install-required` is the one that earns its own value rather than collapsing into
 * `unsupported`, and it is the single most important line this feature renders. iOS
 * and iPadOS expose `PushManager` **only** to a web app that has been added to the
 * Home Screen: in a Safari tab there is no push and no prompt, and every other
 * platform's advice -- allow notifications, check site settings -- is wrong there. A
 * person told "not supported" on an iPhone would conclude AgentJobs cannot do this,
 * when it can, after one gesture they were never asked to make.
 */
export function pushAvailability(env: PushEnvironment): PushAvailability {
  if (!env.hasServiceWorker || !env.hasNotification) return "unsupported";
  if (!env.hasPushManager) {
    return env.isApplePlatform && !env.isStandalone ? "install-required" : "unsupported";
  }
  if (env.permission === "denied") return "blocked";
  return "available";
}

/**
 * Read the environment from the globals, defensively.
 *
 * `matchMedia` and `navigator.standalone` are both consulted because they answer for
 * different platforms: the media query is the standard, and the non-standard property
 * is what older iOS actually sets.
 */
export function readEnvironment(scope?: Partial<Window> & { navigator?: Navigator }): PushEnvironment {
  const view = (scope ?? (typeof window === "undefined" ? undefined : window)) as
    | (Window & {
        navigator: Navigator & { standalone?: boolean };
        Notification?: { permission: NotificationPermission };
      })
    | undefined;
  if (!view) {
    return {
      hasServiceWorker: false,
      hasPushManager: false,
      hasNotification: false,
      permission: null,
      isApplePlatform: false,
      isStandalone: false,
      isHandheld: false,
    };
  }
  const nav = view.navigator;
  const ua = nav?.userAgent ?? "";
  let standalone = Boolean(nav && (nav as { standalone?: boolean }).standalone);
  try {
    standalone =
      standalone || Boolean(view.matchMedia?.("(display-mode: standalone)")?.matches);
  } catch {
    // A browser that throws on an unknown media query keeps whatever the property said.
  }
  // `MacIntel` with a touch screen is an iPad claiming to be a Mac, which is the
  // default on iPadOS and the reason platform strings are not enough on their own.
  const applePlatform =
    /iPhone|iPad|iPod/.test(ua) || (/Macintosh/.test(ua) && (nav?.maxTouchPoints ?? 0) > 1);
  // Client hints first: `userAgentData.mobile` is the browser answering the question
  // directly rather than us inferring it from a string it also controls. Chromium-only,
  // so the user-agent test remains the fallback, and an iPad reaches it through
  // `applePlatform` because its own string says Macintosh.
  const hinted = (nav as Navigator & { userAgentData?: { mobile?: boolean } })?.userAgentData;
  const handheld =
    typeof hinted?.mobile === "boolean"
      ? hinted.mobile
      : /Android|iPhone|iPod|Mobile/.test(ua) || applePlatform;
  return {
    hasServiceWorker: Boolean(nav && "serviceWorker" in nav),
    hasPushManager: "PushManager" in view,
    hasNotification: Boolean(view.Notification),
    permission: view.Notification ? view.Notification.permission : null,
    isApplePlatform: applePlatform,
    isStandalone: standalone,
    isHandheld: handheld,
  };
}

/**
 * A name a person will recognise their own device by.
 *
 * The endpoint is never shown -- it is a capability to notify, not an identifier -- so
 * this string and the push service's host are the whole of what distinguishes two rows.
 * Coarse on purpose: "Android phone" is enough to pick out of a list of three, and
 * parsing a user-agent finely enough to say the model is a losing game.
 */
export function deviceLabel(userAgent: string): string {
  if (/iPad/.test(userAgent)) return "iPad";
  if (/iPhone/.test(userAgent)) return "iPhone";
  if (/Android/.test(userAgent)) return /Mobile/.test(userAgent) ? "Android phone" : "Android tablet";
  if (/Macintosh/.test(userAgent)) return "Mac";
  if (/Windows/.test(userAgent)) return "Windows desktop";
  if (/Linux/.test(userAgent)) return "Linux desktop";
  return "This device";
}

type SubscriptionJson = { endpoint: string; keys?: { p256dh?: string; auth?: string } };

/**
 * Turn what `PushSubscription.toJSON()` gives into what the API takes, or `null`.
 *
 * `null` rather than a throw for a subscription missing its keys, which is a real
 * state: a browser can hand back a subscription created before the page asked for
 * `userVisibleOnly`, and it is unusable rather than exceptional.
 */
export function subscribePayload(
  json: SubscriptionJson,
  { label, detail = DETAIL_TASK }: { label: string; detail?: string },
): { endpoint: string; keys: { p256dh: string; auth: string }; label: string; detail: string } | null {
  const p256dh = json.keys?.p256dh;
  const auth = json.keys?.auth;
  if (!json.endpoint || !p256dh || !auth) return null;
  return { endpoint: json.endpoint, keys: { p256dh, auth }, label, detail };
}

type CacheLike = {
  put: (request: string, response: Response) => Promise<void>;
  delete?: (request: string) => Promise<boolean>;
};
type CachesLike = { open: (name: string) => Promise<CacheLike> };

/**
 * Leave the service worker what it needs to re-subscribe without a page.
 *
 * Failures are swallowed: the cost of not writing it is that a browser rotating this
 * subscription goes quiet until somebody opens AgentJobs again, which is a degraded
 * feature rather than a broken page.
 */
export async function writePushContext(
  context: { projectId: string; applicationServerKey: string; label: string; detail: string },
  caches?: CachesLike | null,
): Promise<void> {
  try {
    const store = caches ?? (typeof window === "undefined" ? undefined : window.caches);
    if (!store) return;
    const cache = await store.open(PUSH_CONTEXT_CACHE);
    await cache.put(PUSH_CONTEXT_KEY, new Response(JSON.stringify(context)));
  } catch {
    // See above.
  }
}

export async function clearPushContext(caches?: CachesLike | null): Promise<void> {
  try {
    const store = caches ?? (typeof window === "undefined" ? undefined : window.caches);
    if (!store) return;
    const cache = await store.open(PUSH_CONTEXT_CACHE);
    await cache.delete?.(PUSH_CONTEXT_KEY);
  } catch {
    // See above.
  }
}

type StorageLike = Pick<Storage, "getItem" | "setItem" | "removeItem">;

export function readDeviceId(storage?: StorageLike | null): string | null {
  try {
    return (storage ?? window.localStorage).getItem(DEVICE_STORAGE_KEY);
  } catch {
    return null;
  }
}

export function writeDeviceId(value: string | null, storage?: StorageLike | null): void {
  try {
    const store = storage ?? window.localStorage;
    if (value === null) store.removeItem(DEVICE_STORAGE_KEY);
    else store.setItem(DEVICE_STORAGE_KEY, value);
  } catch {
    // Losing this costs the Remove button its precise row; unsubscribing by endpoint
    // still works, because the browser knows its own endpoint.
  }
}
