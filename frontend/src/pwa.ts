import { hasUnsentComposition } from "./unsentComposition";

type ServiceWorkerLike = Pick<ServiceWorkerContainer, "addEventListener" | "controller" | "register">;

/**
 * Reload the tab when a new service worker takes control -- unless somebody is typing.
 *
 * **Why a reload happens at all.** The packaged worker calls `skipWaiting()` on install
 * and `clients.claim()` on activate, so a frontend rebuild takes over every open tab as
 * soon as the browser notices the new `sw.js`. That tab is then being served a new
 * bundle's assets while running the old bundle's JavaScript, which is a genuinely bad
 * state -- so the reload stays, and this is not a switch for turning it off.
 *
 * **Why it now asks first.** Nothing about it was ever announced: no prompt, no banner,
 * no gesture. Observed in a real browser for task-512 -- a half-typed finding in the
 * open capture dialog, gone, at a moment the browser chose rather than the person. The
 * update check's timing is not ours, which made it unpredictable rather than rare.
 *
 * So a controller change arriving while anything holds unsent text is declined here,
 * and `VersionSkew` -- which polls the served bundle id and is the path that has always
 * asked -- puts up its dismissible banner with the Reload button instead. The decision
 * moves to the person; it does not disappear.
 *
 * **Declining is not deferring.** Reloading the instant the last character is deleted
 * would be the same ambush with better timing. The tab stays where it is and the banner
 * stands. `refreshing` is deliberately left false, so a later controller change on an
 * idle tab still reloads exactly the way it always did.
 */
export function installControllerReload(
  serviceWorker: ServiceWorkerLike,
  reload: () => void = () => window.location.reload(),
  unsent: () => boolean = hasUnsentComposition,
) {
  let controlled = Boolean(serviceWorker.controller);
  let refreshing = false;
  serviceWorker.addEventListener("controllerchange", () => {
    if (!controlled) {
      controlled = true;
      return;
    }
    if (refreshing) return;
    if (unsent()) return;
    refreshing = true;
    reload();
  });
}

export function registerPwa() {
  if (!("serviceWorker" in navigator)) return;
  installControllerReload(navigator.serviceWorker);
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/app/sw.js", { scope: "/app/" }).catch((error) => {
      console.error("AgentJobs service worker registration failed", error);
    });
  });
}
