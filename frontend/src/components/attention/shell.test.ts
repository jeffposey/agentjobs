import { describe, expect, it, vi } from "vitest";

import type { AttentionNotification } from "./episode";
import {
  BADGE_MAX,
  QUIET_FAVICON,
  applyAppBadge,
  attentionFaviconHref,
  deliver,
  paintFavicon,
  shouldRenotify,
} from "./shell";

/**
 * The three shell affordances, each asserted through its seam (task-422).
 *
 * None of them is allowed to throw, because all three are optional at runtime: the
 * badge does not exist outside an installed app, the favicon is cosmetic, and a
 * notification can be refused by a permission or swallowed by Windows Focus Assist.
 * What is asserted is the *value handed to the platform* and the outcome reported
 * back -- never that a pixel appeared, which no browser API tells us.
 */

const note: AttentionNotification = {
  episodeId: "att_one",
  title: "2 tasks are waiting on you",
  body: "task-001: Review the branch — and 1 other.",
  tag: "agentjobs-attention-agentjobs",
  url: "/app/p/agentjobs/tasks?status=attention&attention_ack=att_one",
};

describe("the taskbar badge", () => {
  it("sets the count while work is waiting", async () => {
    const setAppBadge = vi.fn().mockResolvedValue(undefined);

    await applyAppBadge(3, { setAppBadge, clearAppBadge: vi.fn() });

    expect(setAppBadge).toHaveBeenCalledWith(3);
  });

  it("clears rather than setting zero", async () => {
    // `setAppBadge(0)` shows a dot in some browsers, which would leave the taskbar
    // saying something is waiting on a day nothing is -- the badge that never reaches
    // zero, one layer down.
    const clearAppBadge = vi.fn().mockResolvedValue(undefined);

    await applyAppBadge(0, { setAppBadge: vi.fn(), clearAppBadge });

    expect(clearAppBadge).toHaveBeenCalled();
  });

  it("caps the number the shell is asked to draw", async () => {
    const setAppBadge = vi.fn().mockResolvedValue(undefined);

    await applyAppBadge(400, { setAppBadge, clearAppBadge: vi.fn() });

    expect(setAppBadge).toHaveBeenCalledWith(BADGE_MAX);
  });

  it("is a no-op where the API does not exist", async () => {
    await expect(applyAppBadge(3, {})).resolves.toBe("unsupported");
  });

  it("swallows a browser that exposes the method and refuses the call", async () => {
    const setAppBadge = vi.fn().mockRejectedValue(new Error("not installed"));

    await expect(
      applyAppBadge(3, { setAppBadge, clearAppBadge: vi.fn() }),
    ).resolves.toBe("unsupported");
  });
});

describe("the tab icon", () => {
  it("is a red disc carrying the number", () => {
    const href = attentionFaviconHref(3);

    expect(href.startsWith("data:image/svg+xml,")).toBe(true);
    // The same red as the in-app badge, so the tab, the header and the dashboard
    // alarm are one colour rather than three.
    expect(decodeURIComponent(href)).toContain("#ef4444");
    expect(decodeURIComponent(href)).toContain(">3<");
  });

  it("caps the label the way the header badge does", () => {
    expect(decodeURIComponent(attentionFaviconHref(14))).toContain(">9+<");
  });

  it("creates the icon link the page never had, and swaps it back when quiet", () => {
    const doc = document.implementation.createHTMLDocument("t");

    paintFavicon(2, doc);
    const link = doc.querySelector('link[rel="icon"]');
    expect(link?.getAttribute("href")).toBe(attentionFaviconHref(2));

    paintFavicon(0, doc);
    expect(doc.querySelector('link[rel="icon"]')?.getAttribute("href")).toBe(QUIET_FAVICON);
    expect(doc.querySelectorAll('link[rel="icon"]')).toHaveLength(1);
  });
});

describe("delivering the notification", () => {
  function permission(value: NotificationPermission) {
    const ctor = vi.fn() as unknown as {
      permission: NotificationPermission;
      new (title: string, options?: NotificationOptions): Notification;
    };
    (ctor as unknown as { permission: NotificationPermission }).permission = value;
    return ctor;
  }

  it("prefers the service worker, so the notification outlives the page", async () => {
    const showNotification = vi.fn().mockResolvedValue(undefined);
    const outcome = await deliver(note, {
      notification: permission("granted"),
      serviceWorker: {
        getRegistration: vi
          .fn()
          .mockResolvedValue({ showNotification } as unknown as ServiceWorkerRegistration),
      },
    });

    expect(outcome).toBe("shown");
    expect(showNotification).toHaveBeenCalledWith(note.title, expect.objectContaining({
      body: note.body,
      tag: note.tag,
      renotify: true,
    }));
  });

  it("carries the destination so the click has somewhere to go", async () => {
    const showNotification = vi.fn().mockResolvedValue(undefined);
    await deliver(note, {
      notification: permission("granted"),
      serviceWorker: {
        getRegistration: vi
          .fn()
          .mockResolvedValue({ showNotification } as unknown as ServiceWorkerRegistration),
      },
    });

    expect(showNotification.mock.calls[0]?.[1]?.data).toEqual({
      url: note.url,
      episodeId: "att_one",
    });
  });

  it("falls back to a page notification when there is no worker", async () => {
    const ctor = permission("granted");

    const outcome = await deliver(note, {
      notification: ctor,
      serviceWorker: { getRegistration: vi.fn().mockResolvedValue(undefined) },
    });

    expect(outcome).toBe("shown");
    expect(ctor).toHaveBeenCalledWith(note.title, expect.objectContaining({ body: note.body }));
  });

  it("reports a refused permission rather than attempting anything", async () => {
    const ctor = permission("denied");

    await expect(deliver(note, { notification: ctor })).resolves.toBe("blocked");
    expect(ctor).not.toHaveBeenCalled();
  });

  it("reports a browser with no notifications at all", async () => {
    await expect(deliver(note, { notification: undefined })).resolves.toBe("unsupported");
  });

  it("reports a failure instead of raising one", async () => {
    const outcome = await deliver(note, {
      notification: permission("granted"),
      serviceWorker: {
        getRegistration: vi.fn().mockRejectedValue(new Error("worker gone")),
      },
    });

    expect(outcome).toBe("failed");
  });

  it("calls a suppressed notification shown, which is the honest answer", async () => {
    /**
     * Windows Focus Assist and Do Not Disturb swallow the banner without telling the
     * page: `showNotification` resolves exactly as it does when the banner appears.
     * So "shown" here means "handed to the shell", and nothing in this feature treats
     * it as evidence a person saw anything -- the episode stays unacknowledged and the
     * red badge stays up, which is what greets them when quiet hours end.
     */
    const quietWindows = vi.fn().mockResolvedValue(undefined);

    const outcome = await deliver(note, {
      notification: permission("granted"),
      serviceWorker: {
        getRegistration: vi.fn().mockResolvedValue({
          showNotification: quietWindows,
        } as unknown as ServiceWorkerRegistration),
      },
    });

    expect(outcome).toBe("shown");
  });
});

describe("whether a replacement interrupts", () => {
  /**
   * The defect this rule exists for, reproduced in the owner's own Chrome (task-421).
   *
   * One tag per project means Windows replaces rather than stacks, and a replacement is
   * silent unless `renotify` asks otherwise. With it fixed at `false`, the first
   * attention toast of all time drew a banner and every later one overwrote an entry
   * nobody had dismissed -- no banner, no sound, for days. Fixed at `true` it would buzz
   * twice for one episode on an installed desktop PWA, which receives both the page's
   * toast and a push. So the question is whether the episode on screen is *this* one.
   */
  const standing = (episodeId: string | null) => [{ data: episodeId ? { episodeId } : {} }];

  it("interrupts when the standing notification is a different episode", () => {
    expect(shouldRenotify(standing("att_old"), "att_new")).toBe(true);
  });

  it("stays quiet when the same episode is being re-drawn", () => {
    expect(shouldRenotify(standing("att_one"), "att_one")).toBe(false);
  });

  it("interrupts when nothing is on screen at all", () => {
    expect(shouldRenotify([], "att_one")).toBe(true);
  });

  it("stays quiet for a notification with no episode", () => {
    // The worker's "nothing is waiting any more" notice. Good news is not an
    // interruption.
    expect(shouldRenotify([], null)).toBe(false);
  });

  it("asks the shell what is standing before it draws", async () => {
    const showNotification = vi.fn().mockResolvedValue(undefined);
    const getNotifications = vi.fn().mockResolvedValue([{ data: { episodeId: "att_one" } }]);

    await deliver(note, {
      notification: { permission: "granted" } as unknown as {
        permission: NotificationPermission;
        new (title: string, options?: NotificationOptions): Notification;
      },
      serviceWorker: {
        getRegistration: vi
          .fn()
          .mockResolvedValue({ showNotification, getNotifications } as unknown as ServiceWorkerRegistration),
      },
    });

    expect(getNotifications).toHaveBeenCalledWith({ tag: note.tag });
    expect(showNotification.mock.calls[0]?.[1]?.renotify).toBe(false);
  });

  it("draws anyway when the browser will not say what is standing", async () => {
    const showNotification = vi.fn().mockResolvedValue(undefined);
    const getNotifications = vi.fn().mockRejectedValue(new Error("no"));

    const outcome = await deliver(note, {
      notification: { permission: "granted" } as unknown as {
        permission: NotificationPermission;
        new (title: string, options?: NotificationOptions): Notification;
      },
      serviceWorker: {
        getRegistration: vi
          .fn()
          .mockResolvedValue({ showNotification, getNotifications } as unknown as ServiceWorkerRegistration),
      },
    });

    expect(outcome).toBe("shown");
    expect(showNotification.mock.calls[0]?.[1]?.renotify).toBe(true);
  });
});
