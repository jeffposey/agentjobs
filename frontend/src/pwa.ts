type ServiceWorkerLike = Pick<ServiceWorkerContainer, "addEventListener" | "controller" | "register">;

export function installControllerReload(
  serviceWorker: ServiceWorkerLike,
  reload: () => void = () => window.location.reload(),
) {
  let controlled = Boolean(serviceWorker.controller);
  let refreshing = false;
  serviceWorker.addEventListener("controllerchange", () => {
    if (!controlled) {
      controlled = true;
      return;
    }
    if (refreshing) return;
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
