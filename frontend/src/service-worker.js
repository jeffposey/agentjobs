const CACHE_NAME = "__CACHE_NAME__";
const SHELL_URLS = __SHELL_URLS__;
const SHELL_PATHS = new Set(SHELL_URLS.map((url) => new URL(url, self.location.origin).pathname));

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL_URLS)).then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((key) => key.startsWith("agentjobs-shell-") && key !== CACHE_NAME)
          .map((key) => caches.delete(key)),
      ))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  // Task data is always network-only. Never make an old assignment look current.
  if (url.pathname.startsWith("/api/")) {
    event.respondWith(fetch(request));
    return;
  }

  if (request.mode === "navigate" && url.pathname.startsWith("/app")) {
    event.respondWith(fetch(request).catch(() => caches.match("/app/")));
    return;
  }

  if (SHELL_PATHS.has(url.pathname)) {
    event.respondWith(caches.match(request).then((cached) => cached || fetch(request)));
  }
});

// Activating an attention notification (task-422).
//
// The handler is here rather than on the page because the case the notification exists
// for is a window that is minimised, behind something, or closed -- and a notification
// owned by a page dies with the page. The worker outlives all three, so the click always
// lands somewhere.
//
// An existing AgentJobs window is reused before a new one is opened. Two windows on the
// same project, one of them showing a task the person was not asked about, is a worse
// answer than moving the one they already have; `navigate()` then takes it to the task
// or the filtered waiting list, carrying the `attention_ack` marker that tells the app
// this click acknowledged the episode.
self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = (event.notification.data && event.notification.data.url) || "/app/";
  event.waitUntil(
    self.clients
      .matchAll({ type: "window", includeUncontrolled: true })
      .then((windows) => {
        for (const client of windows) {
          if (new URL(client.url).pathname.startsWith("/app")) {
            // Navigate first, focus second: a client focused and then navigated shows
            // the old page for a frame, which reads as the click having gone nowhere.
            return Promise.resolve(client.navigate ? client.navigate(target) : client)
              .then((navigated) => (navigated || client).focus())
              .catch(() => client.focus());
          }
        }
        return self.clients.openWindow(target);
      }),
  );
});

// ---------------------------------------------------------------------------
// Mobile push (task-423).
//
// A push arrives at a worker, not at a page, and usually at a device whose AgentJobs
// window was closed hours ago. So everything below has to work with no application
// running: parse the payload, decide what to say, and say it.
//
// **The payload is a starting point, not the message.** A push service holds a
// message for an offline device for up to its TTL, which here is twelve hours, and
// "3 tasks are waiting on you" is a sentence that can stop being true in that time.
// So the worker re-reads the live attention state and renders *that*, falling back to
// the payload only when the fetch fails -- which is the offline case, where the
// payload is the best that exists.
//
// A `push` handler that shows nothing is penalised by the browser, so every branch
// here ends in a notification, including the one where the answer turns out to be
// "nothing is waiting any more".

const PUSH_CONTEXT_CACHE = "agentjobs-push-context";
const PUSH_CONTEXT_KEY = "/__agentjobs_push_context__";

function pushFallback(payload) {
  return {
    title: (payload && payload.title) || "AgentJobs needs you",
    body: (payload && payload.body) || "Open AgentJobs to see what has stopped.",
    url: (payload && payload.url) || "/app/",
    tag: (payload && payload.tag) || "agentjobs-attention",
    episodeId: (payload && payload.episode) || null,
  };
}

// The current truth, or null when this device cannot reach the server right now.
async function currentAttention(projectId) {
  if (!projectId) return null;
  try {
    const response = await fetch(`/api/projects/${encodeURIComponent(projectId)}/attention`, {
      credentials: "same-origin",
      cache: "no-store",
    });
    if (!response.ok) return null;
    return await response.json();
  } catch {
    return null;
  }
}

// Whether this push is owed a *fresh* interruption, given what is already on screen.
//
// The same rule as `shouldRenotify` in `components/attention/shell.ts`, duplicated
// because this file is plain JavaScript shipped whole and cannot import it;
// `push.test.ts` reads this source and asserts the two still agree. A tagged
// notification replaces its predecessor silently unless `renotify` says otherwise, so
// a fixed `false` meant every attention notification after the very first one
// overwrote an undismissed entry with no banner and no sound (task-421). An episode
// already on screen is an update of a number and stays quiet; anything else is a new
// run of attention. No episode at all is the "nothing is waiting any more" notice,
// which is good news and not worth waking anybody for.
function renotifyFor(standing, episodeId) {
  if (!episodeId) return false;
  return !standing.some((entry) => entry.data && entry.data.episodeId === episodeId);
}

async function standingFor(tag) {
  try {
    return (await self.registration.getNotifications({ tag })) || [];
  } catch {
    return [];
  }
}

function notificationFromAttention(attention, payload) {
  const count = attention.blocking || 0;
  const episode = attention.episode;
  if (count <= 0 || !episode) {
    // The push outlived the work. Saying so is better than repeating a number that is
    // no longer true, and better than the browser's own "this site was updated in the
    // background", which is what showing nothing would earn.
    return {
      title: "AgentJobs is clear",
      body: "Nothing is waiting on you any more.",
      url: "/app/",
      tag: pushFallback(payload).tag,
      episodeId: null,
    };
  }
  const title = count === 1 ? "1 task is waiting on you" : `${count} tasks are waiting on you`;
  return {
    title,
    // The body stays the server's, because it is the only part that depends on the
    // device's own privacy setting -- whether this device is allowed to be told which
    // task it is. Re-deriving it here would silently promote every device to the
    // detailed form.
    body: pushFallback(payload).body,
    url: episode.deep_link || pushFallback(payload).url,
    tag: pushFallback(payload).tag,
    // The live episode rather than the payload's: a push held for twelve hours can
    // arrive after the episode it was sent for has been replaced, and it is the one on
    // screen now that decides whether this is an update or a new interruption.
    episodeId: episode.id || pushFallback(payload).episodeId,
  };
}

self.addEventListener("push", (event) => {
  let payload = null;
  try {
    payload = event.data ? event.data.json() : null;
  } catch {
    payload = null;
  }
  event.waitUntil(
    (async () => {
      const attention = await currentAttention(payload && payload.project);
      const note = attention ? notificationFromAttention(attention, payload) : pushFallback(payload);
      const standing = await standingFor(note.tag);
      return self.registration.showNotification(note.title, {
        body: note.body,
        tag: note.tag,
        renotify: renotifyFor(standing, note.episodeId),
        // The episode rides in `data` so the next notification under this tag can tell
        // an update from a new run of attention -- the page's own toast writes the same
        // key, because on an installed desktop PWA both channels draw here.
        data: { url: note.url, episodeId: note.episodeId },
        icon: "/app/icons/icon-192.png",
        badge: "/app/icons/icon-192.png",
      });
    })(),
  );
});

// The browser rotating a subscription out from under us.
//
// It fires once, possibly while no page has been open for weeks, and if it is not
// handled the device goes quiet for ever: the old endpoint starts answering 410 and
// nothing ever registers the new one. The page cannot do this on its own because the
// case that matters is the one where the page is never opened again.
//
// The project id is not derivable here, so the page leaves it in a cache entry when it
// subscribes. A worker with no such entry has nothing to re-register against and stops,
// which is the same state as never having subscribed.
async function pushContext() {
  try {
    const cache = await caches.open(PUSH_CONTEXT_CACHE);
    const stored = await cache.match(PUSH_CONTEXT_KEY);
    return stored ? await stored.json() : null;
  } catch {
    return null;
  }
}

self.addEventListener("pushsubscriptionchange", (event) => {
  event.waitUntil(
    (async () => {
      const context = await pushContext();
      if (!context || !context.projectId || !context.applicationServerKey) return;
      const base = `/api/projects/${encodeURIComponent(context.projectId)}/push`;
      const post = (path, body) =>
        fetch(base + path, {
          method: "POST",
          credentials: "same-origin",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        }).catch(() => undefined);

      const old = event.oldSubscription;
      if (old && old.endpoint) await post("/unsubscribe", { endpoint: old.endpoint });

      let fresh = event.newSubscription || null;
      if (!fresh) {
        try {
          fresh = await self.registration.pushManager.subscribe({
            userVisibleOnly: true,
            applicationServerKey: context.applicationServerKey,
          });
        } catch {
          return;
        }
      }
      const json = fresh.toJSON();
      await post("/subscribe", {
        endpoint: json.endpoint,
        keys: json.keys,
        label: context.label || "",
        detail: context.detail || "count",
      });
    })(),
  );
});
