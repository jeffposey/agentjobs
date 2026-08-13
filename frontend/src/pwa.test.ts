import { describe, expect, it, vi } from "vitest";

import { installControllerReload } from "./pwa";

function serviceWorker(initiallyControlled: boolean) {
  let controllerChange: (() => void) | undefined;
  return {
    container: {
      controller: initiallyControlled ? ({} as ServiceWorker) : null,
      register: vi.fn(),
      addEventListener: vi.fn((event: string, listener: EventListenerOrEventListenerObject) => {
        if (event === "controllerchange") controllerChange = listener as () => void;
      }),
    },
    changeController: () => controllerChange?.(),
  };
}

describe("service-worker upgrades", () => {
  it("reloads exactly once when a new worker replaces the installed controller", () => {
    const worker = serviceWorker(true);
    const reload = vi.fn();
    installControllerReload(worker.container, reload);

    worker.changeController();
    worker.changeController();

    expect(reload).toHaveBeenCalledTimes(1);
  });

  it("does not reload merely because the first worker takes control", () => {
    const worker = serviceWorker(false);
    const reload = vi.fn();
    installControllerReload(worker.container, reload);

    worker.changeController();

    expect(reload).not.toHaveBeenCalled();
  });
});
