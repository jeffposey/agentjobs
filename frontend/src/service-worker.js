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
