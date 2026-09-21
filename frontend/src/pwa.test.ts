import { afterEach, describe, expect, it, vi } from "vitest";

import { installControllerReload } from "./pwa";
import { clearUnsentComposition, setUnsentComposition } from "./unsentComposition";

afterEach(clearUnsentComposition);

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
  /** Nobody is typing, which is the state every pre-existing test here assumed. */
  const idle = () => false;

  it("reloads exactly once when a new worker replaces the installed controller", () => {
    const worker = serviceWorker(true);
    const reload = vi.fn();
    installControllerReload(worker.container, reload, idle);

    worker.changeController();
    worker.changeController();

    expect(reload).toHaveBeenCalledTimes(1);
  });

  it("does not reload merely because the first worker takes control", () => {
    const worker = serviceWorker(false);
    const reload = vi.fn();
    installControllerReload(worker.container, reload, idle);

    worker.changeController();

    expect(reload).not.toHaveBeenCalled();
  });
});

describe("a controller change while somebody is typing", () => {
  it("does not reload the page out from under unsent text", () => {
    // The defect this guards, observed in a real browser for task-512: a rebuild took
    // the tab, and the half-written finding in the open capture dialog with it. The
    // person was asked nothing.
    const worker = serviceWorker(true);
    const reload = vi.fn();
    installControllerReload(worker.container, reload, () => true);

    worker.changeController();

    expect(reload).not.toHaveBeenCalled();
  });

  it("asks again next time rather than firing the moment the text is gone", () => {
    // Declining is not deferring. Reloading the instant the last character is deleted
    // would be the same ambush with better timing, so the deferred reload is simply
    // dropped -- `VersionSkew` is what offers it -- and the next controller change on
    // an idle tab is treated exactly as it always was.
    const worker = serviceWorker(true);
    const reload = vi.fn();
    let typing = true;
    installControllerReload(worker.container, reload, () => typing);

    worker.changeController();
    typing = false;
    expect(reload).not.toHaveBeenCalled();

    worker.changeController();
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it("asks the guard afresh each time, not once at install", () => {
    const worker = serviceWorker(true);
    const reload = vi.fn();
    const unsent = vi.fn(() => false);
    installControllerReload(worker.container, reload, unsent);

    worker.changeController();

    expect(unsent).toHaveBeenCalledTimes(1);
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it("defaults to the page's own composition registry", () => {
    // No third argument: this is the call `registerPwa` makes, and the default has to
    // be the live registry or the guard is wired to nothing in production.
    const worker = serviceWorker(true);
    const reload = vi.fn();
    installControllerReload(worker.container, reload);

    setUnsentComposition("capture", true);
    worker.changeController();
    expect(reload).not.toHaveBeenCalled();

    clearUnsentComposition();
    worker.changeController();
    expect(reload).toHaveBeenCalledTimes(1);
  });
});
