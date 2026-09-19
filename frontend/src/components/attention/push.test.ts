import { describe, expect, it, vi } from "vitest";

// The worker's own source, as a string. `?raw` rather than `node:fs` because this
// project has no Node types, and the same trick `PrimaryNav.test.tsx` already uses.
import serviceWorkerSource from "../../service-worker.js?raw";

import {
  DEVICE_STORAGE_KEY,
  PUSH_CONTEXT_CACHE,
  PUSH_CONTEXT_KEY,
  clearPushContext,
  deviceLabel,
  pushAvailability,
  readDeviceId,
  readEnvironment,
  subscribePayload,
  writeDeviceId,
  writePushContext,
  type PushEnvironment,
} from "./push";

/**
 * What a browser can do about push, decided as a function of data (task-423).
 *
 * Every case here is one that cannot be reproduced by running the test: an iPhone in a
 * Safari tab, a browser with no `PushManager`, a permission already refused. That is
 * precisely why the decision is a pure function of an environment record rather than a
 * pile of `typeof` checks inside a component.
 */

function env(overrides: Partial<PushEnvironment> = {}): PushEnvironment {
  return {
    hasServiceWorker: true,
    hasPushManager: true,
    hasNotification: true,
    permission: "default",
    isApplePlatform: false,
    isStandalone: false,
    ...overrides,
  };
}

describe("what this device can do", () => {
  it("is available on an ordinary supporting browser", () => {
    expect(pushAvailability(env())).toBe("available");
    expect(pushAvailability(env({ permission: "granted" }))).toBe("available");
  });

  it("separates a refused permission from a browser that cannot", () => {
    // Different sentences, because one is a decision the person can reverse and the
    // other is not. Telling somebody to check a setting that does not exist wastes
    // their time; telling somebody it is impossible when they only said no once loses
    // the feature.
    expect(pushAvailability(env({ permission: "denied" }))).toBe("blocked");
    expect(pushAvailability(env({ hasNotification: false }))).toBe("unsupported");
    expect(pushAvailability(env({ hasServiceWorker: false }))).toBe("unsupported");
  });

  it("tells an iPhone in a browser tab to install the app first", () => {
    // The single most important line this feature renders. iOS exposes `PushManager`
    // only to a Home Screen web app, so "not supported" here would be false -- and the
    // person would never make the one gesture that makes it true.
    expect(
      pushAvailability(env({ hasPushManager: false, isApplePlatform: true, isStandalone: false })),
    ).toBe("install-required");
  });

  it("does not offer that advice to an installed app that still cannot push", () => {
    expect(
      pushAvailability(env({ hasPushManager: false, isApplePlatform: true, isStandalone: true })),
    ).toBe("unsupported");
    expect(pushAvailability(env({ hasPushManager: false }))).toBe("unsupported");
  });
});

describe("reading the environment", () => {
  it("recognises an iPad pretending to be a Mac", () => {
    // iPadOS reports `Macintosh` by default, so the platform string alone is wrong on
    // the one device whose answer differs from every other desktop.
    const scope = {
      navigator: {
        userAgent: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Safari/605.1.15",
        maxTouchPoints: 5,
      },
      matchMedia: () => ({ matches: false }),
    } as unknown as Window;

    expect(readEnvironment(scope).isApplePlatform).toBe(true);
  });

  it("treats a real Mac as a desktop", () => {
    const scope = {
      navigator: { userAgent: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)", maxTouchPoints: 0 },
      matchMedia: () => ({ matches: false }),
    } as unknown as Window;

    expect(readEnvironment(scope).isApplePlatform).toBe(false);
  });

  it("survives a browser with no globals at all", () => {
    expect(readEnvironment(undefined as never).hasPushManager).toBe(false);
  });

  /**
   * Which of the two notification panels a device is offered (task-421). Client hints
   * are preferred because they are the browser answering the question rather than us
   * inferring it from a string the same browser controls; the user-agent cases are the
   * fallback for browsers that do not send them.
   */
  describe("telling a handheld from a desktop", () => {
    function scopeFor(userAgent: string, extras: Record<string, unknown> = {}) {
      return {
        navigator: { userAgent, maxTouchPoints: 0, ...extras },
        matchMedia: () => ({ matches: false }),
      } as unknown as Window;
    }

    it("believes a client hint over the user-agent string", () => {
      const scope = scopeFor("Mozilla/5.0 (Windows NT 10.0; Win64; x64)", {
        userAgentData: { mobile: true },
      });
      expect(readEnvironment(scope).isHandheld).toBe(true);
    });

    it("believes a client hint that says desktop", () => {
      const scope = scopeFor("Mozilla/5.0 (Linux; Android 14; Pixel 9) Mobile", {
        userAgentData: { mobile: false },
      });
      expect(readEnvironment(scope).isHandheld).toBe(false);
    });

    it.each([
      ["Mozilla/5.0 (Linux; Android 14; Pixel 9) Mobile Chrome", true],
      ["Mozilla/5.0 (iPhone; CPU iPhone OS 17_0) Safari", true],
      ["Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome", false],
      ["Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)", false],
    ])("falls back to the user agent: %s", (ua, expected) => {
      expect(readEnvironment(scopeFor(ua)).isHandheld).toBe(expected);
    });

    it("counts an iPad that calls itself a Mac as a handheld", () => {
      const scope = scopeFor("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)", {
        maxTouchPoints: 5,
      });
      expect(readEnvironment(scope).isHandheld).toBe(true);
    });
  });
});

describe("naming a device", () => {
  it.each([
    ["Mozilla/5.0 (iPhone; CPU iPhone OS 17_0) Safari", "iPhone"],
    ["Mozilla/5.0 (iPad; CPU OS 17_0) Safari", "iPad"],
    ["Mozilla/5.0 (Linux; Android 14; Pixel 9) Mobile Chrome", "Android phone"],
    ["Mozilla/5.0 (Linux; Android 14; Tab) Chrome", "Android tablet"],
    ["Mozilla/5.0 (Windows NT 10.0; Win64) Chrome", "Windows desktop"],
    ["something else entirely", "This device"],
  ])("%s reads as %s", (agent, expected) => {
    expect(deviceLabel(agent)).toBe(expected);
  });
});

describe("what gets sent to the API", () => {
  it("passes the browser's own object through", () => {
    expect(
      subscribePayload(
        { endpoint: "https://fcm.example/send/1", keys: { p256dh: "pub", auth: "sec" } },
        { label: "Pixel" },
      ),
    ).toEqual({
      endpoint: "https://fcm.example/send/1",
      keys: { p256dh: "pub", auth: "sec" },
      label: "Pixel",
      detail: "count",
    });
  });

  it("refuses a subscription with no keys rather than throwing", () => {
    // A real state, not an exceptional one: a browser can hand back a subscription
    // created before this page asked for `userVisibleOnly`.
    expect(subscribePayload({ endpoint: "https://a/1" }, { label: "x" })).toBeNull();
    expect(
      subscribePayload({ endpoint: "https://a/1", keys: { p256dh: "pub" } }, { label: "x" }),
    ).toBeNull();
  });
});

describe("what the service worker is left", () => {
  it("writes the context it needs to re-subscribe alone", async () => {
    const put = vi.fn().mockResolvedValue(undefined);
    await writePushContext(
      { projectId: "inbox", applicationServerKey: "KEY", label: "Pixel", detail: "count" },
      { open: async () => ({ put }) },
    );

    expect(put).toHaveBeenCalledOnce();
    const [key, response] = put.mock.calls[0] as [string, Response];
    expect(key).toBe(PUSH_CONTEXT_KEY);
    await expect(response.json()).resolves.toEqual({
      projectId: "inbox",
      applicationServerKey: "KEY",
      label: "Pixel",
      detail: "count",
    });
  });

  it("swallows a cache that will not open", async () => {
    // Losing this degrades the feature -- a rotated subscription goes quiet until
    // somebody opens AgentJobs again -- and must never break the page that was
    // otherwise succeeding at registering a device.
    await expect(
      writePushContext(
        { projectId: "inbox", applicationServerKey: "K", label: "", detail: "count" },
        { open: async () => { throw new Error("no storage"); } },
      ),
    ).resolves.toBeUndefined();
    await expect(
      clearPushContext({ open: async () => { throw new Error("no storage"); } }),
    ).resolves.toBeUndefined();
  });

  it("agrees with the service worker about where it is", () => {
    // `service-worker.js` is plain JavaScript shipped whole and cannot import this
    // module, so the two copies of these constants are checked against each other
    // rather than assumed. A rename on one side alone fails here instead of producing
    // a device that silently stops being woken after its subscription rotates.
    const source = serviceWorkerSource;
    expect(source).toContain(`"${PUSH_CONTEXT_CACHE}"`);
    expect(source).toContain(`"${PUSH_CONTEXT_KEY}"`);
    expect(source).toContain("pushsubscriptionchange");
    expect(source).toContain("addEventListener(\"push\"");
  });
});

describe("remembering this device's row", () => {
  it("round-trips through storage and can be cleared", () => {
    const store = new Map<string, string>();
    const storage = {
      getItem: (key: string) => store.get(key) ?? null,
      setItem: (key: string, value: string) => void store.set(key, value),
      removeItem: (key: string) => void store.delete(key),
    };

    writeDeviceId("dev_one", storage);
    expect(store.get(DEVICE_STORAGE_KEY)).toBe("dev_one");
    expect(readDeviceId(storage)).toBe("dev_one");
    writeDeviceId(null, storage);
    expect(readDeviceId(storage)).toBeNull();
  });

  it("answers null when storage throws", () => {
    // A private window or blocked site data. The cost of losing this is the Remove
    // button falling back to the endpoint, which the browser knows anyway.
    const hostile = {
      getItem: () => {
        throw new Error("blocked");
      },
      setItem: () => {
        throw new Error("blocked");
      },
      removeItem: () => {},
    };
    expect(readDeviceId(hostile)).toBeNull();
    expect(() => writeDeviceId("x", hostile)).not.toThrow();
  });
});
