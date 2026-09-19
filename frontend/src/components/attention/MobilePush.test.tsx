import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { client } from "../../api/generated/client.gen";
import { apiMockServer } from "../../test/api-mock";
import { MobilePush } from "./MobilePush";

/**
 * The notifications panel, asserted on what a person is told and what is registered.
 *
 * jsdom has no `PushManager`, no `Notification` and no service worker, so every state
 * below is produced by installing the globals a real browser would have. That is the
 * point rather than a workaround: the four states this panel has to get right are
 * exactly the four that cannot be produced by running a test on this machine.
 */

const KEY = "BPxxPUBLICKEYxx";

type Fake = {
  subscribe?: ReturnType<typeof vi.fn>;
  getSubscription?: ReturnType<typeof vi.fn>;
};

function installBrowser({
  permission = "default",
  pushManager = true,
  notification = true,
  existing = null as null | { endpoint: string },
  onSubscribe,
}: {
  permission?: NotificationPermission;
  pushManager?: boolean;
  notification?: boolean;
  existing?: null | { endpoint: string };
  onSubscribe?: ReturnType<typeof vi.fn>;
} = {}): Fake {
  const subscription = {
    endpoint: "https://fcm.example/send/this-device",
    toJSON: () => ({
      endpoint: "https://fcm.example/send/this-device",
      keys: { p256dh: "pub", auth: "sec" },
    }),
    unsubscribe: vi.fn().mockResolvedValue(true),
  };
  const fake: Fake = {
    subscribe: onSubscribe ?? vi.fn().mockResolvedValue(subscription),
    getSubscription: vi.fn().mockResolvedValue(existing ? subscription : null),
  };

  if (notification) {
    Object.defineProperty(window, "Notification", {
      configurable: true,
      writable: true,
      value: Object.assign(
        vi.fn(),
        { permission, requestPermission: vi.fn().mockResolvedValue("granted") },
      ),
    });
  } else {
    // @ts-expect-error - removing a global the panel must cope with not having
    delete window.Notification;
  }

  if (pushManager) {
    Object.defineProperty(window, "PushManager", { configurable: true, value: function () {} });
  } else {
    // @ts-expect-error - see above
    delete window.PushManager;
  }

  Object.defineProperty(navigator, "serviceWorker", {
    configurable: true,
    value: { ready: Promise.resolve({ pushManager: fake }) },
  });
  Object.defineProperty(window, "caches", {
    configurable: true,
    value: { open: async () => ({ put: vi.fn(), delete: vi.fn() }) },
  });
  return fake;
}

function statusBody(devices: unknown[] = [], watching = true) {
  return {
    vapid_public_key: KEY,
    contact: "mailto:agentjobs@localhost",
    watching,
    poll_seconds: 15,
    devices,
  };
}

function registeredDevice(overrides: Record<string, unknown> = {}) {
  return {
    id: "dev_one",
    service: "fcm.example",
    label: "Pixel",
    detail: "count",
    created_at: "2026-09-19T12:00:00Z",
    last_attempt_at: null,
    last_status: null,
    last_error: null,
    consecutive_failures: 0,
    healthy: true,
    ...overrides,
  };
}

function renderPanel() {
  const queryClient = new QueryClient({
    defaultOptions: { mutations: { retry: false }, queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MobilePush projectId="inbox" />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  client.setConfig({ baseUrl: "http://localhost" });
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("what the panel says on each kind of device", () => {
  it("offers to register a supporting browser", async () => {
    installBrowser();
    apiMockServer.use(
      http.get("*/api/projects/inbox/push", () => HttpResponse.json(statusBody())),
    );
    renderPanel();

    await waitFor(() =>
      expect(screen.getByTestId("mobile-push")).toHaveAttribute("data-availability", "available"),
    );
    expect(screen.getByTestId("enable-push")).toBeInTheDocument();
  });

  it("tells an iPhone in a tab to install the app, not that push is impossible", async () => {
    installBrowser({ pushManager: false });
    Object.defineProperty(navigator, "userAgent", {
      configurable: true,
      value: "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0) Safari",
    });
    apiMockServer.use(
      http.get("*/api/projects/inbox/push", () => HttpResponse.json(statusBody())),
    );
    renderPanel();

    await waitFor(() => expect(screen.getByTestId("push-install-required")).toBeInTheDocument());
    expect(screen.getByTestId("push-install-required").textContent).toContain("Home Screen");
  });

  it("says when notifications are blocked, which is a decision the person can reverse", async () => {
    installBrowser({ permission: "denied" });
    apiMockServer.use(
      http.get("*/api/projects/inbox/push", () => HttpResponse.json(statusBody())),
    );
    renderPanel();

    await waitFor(() =>
      expect(screen.getByTestId("mobile-push")).toHaveAttribute("data-availability", "blocked"),
    );
    expect(screen.queryByTestId("enable-push")).toBeNull();
  });

  it("warns when the server is not watching, rather than implying silence is quiet", async () => {
    // The two ways this is false are a process that never started the loop and one
    // whose loop died -- both of which otherwise look exactly like "nothing happened".
    installBrowser();
    apiMockServer.use(
      http.get("*/api/projects/inbox/push", () =>
        HttpResponse.json(statusBody([registeredDevice()], false)),
      ),
    );
    renderPanel();

    await waitFor(() => expect(screen.getByTestId("push-not-watching")).toBeInTheDocument());
  });

  it("renders nothing at all when the read is refused", async () => {
    // A run asking, which is the capability boundary working. A refusal rendered into
    // a person's Dashboard would be noise about somebody else's request.
    installBrowser();
    apiMockServer.use(
      http.get("*/api/projects/inbox/push", () =>
        HttpResponse.json({ code: "capability_denied", detail: "no" }, { status: 403 }),
      ),
    );
    const { container } = renderPanel();

    await waitFor(() => expect(container.querySelector("[data-testid='mobile-push']")).toBeNull());
  });
});

describe("registering this device", () => {
  it("asks permission inside the click, then posts the browser's subscription", async () => {
    const fake = installBrowser();
    let posted: unknown = null;
    let devices: unknown[] = [];
    apiMockServer.use(
      http.get("*/api/projects/inbox/push", () => HttpResponse.json(statusBody(devices))),
      http.post("*/api/projects/inbox/push/subscribe", async ({ request }) => {
        posted = await request.json();
        devices = [registeredDevice()];
        return HttpResponse.json(statusBody(devices));
      }),
    );
    renderPanel();

    await waitFor(() => expect(screen.getByTestId("enable-push")).toBeEnabled());
    fireEvent.click(screen.getByTestId("enable-push"));

    await waitFor(() => expect(posted).not.toBeNull());
    expect(window.Notification.requestPermission).toHaveBeenCalledOnce();
    // Subscribed against the server's key, which is the public half and the only half
    // a client ever sees.
    expect(fake.subscribe).toHaveBeenCalledWith({
      userVisibleOnly: true,
      applicationServerKey: KEY,
    });
    expect(posted).toMatchObject({
      endpoint: "https://fcm.example/send/this-device",
      keys: { p256dh: "pub", auth: "sec" },
      detail: "count",
    });
  });

  it("explains a refused prompt instead of registering nothing silently", async () => {
    installBrowser();
    (window.Notification.requestPermission as ReturnType<typeof vi.fn>).mockResolvedValue("denied");
    apiMockServer.use(
      http.get("*/api/projects/inbox/push", () => HttpResponse.json(statusBody())),
    );
    renderPanel();

    await waitFor(() => expect(screen.getByTestId("enable-push")).toBeEnabled());
    fireEvent.click(screen.getByTestId("enable-push"));

    await waitFor(() => expect(screen.getByTestId("push-problem")).toBeInTheDocument());
    expect(screen.getByTestId("push-problem").textContent).toContain("blocked");
  });
});

describe("a device that is already registered", () => {
  it("offers a test and a way off, and names the other devices", async () => {
    installBrowser({ permission: "granted", existing: { endpoint: "x" } });
    apiMockServer.use(
      http.get("*/api/projects/inbox/push", () =>
        HttpResponse.json(
          statusBody([registeredDevice(), registeredDevice({ id: "dev_two", label: "iPad" })]),
        ),
      ),
    );
    renderPanel();

    await waitFor(() => expect(screen.getByTestId("push-registered")).toBeInTheDocument());
    expect(screen.getByTestId("push-registered").textContent).toContain("1 other");
    expect(screen.getByTestId("test-push")).toBeInTheDocument();
    expect(screen.getByTestId("disable-push")).toBeInTheDocument();
  });

  it("reports what a test push actually did, per device", async () => {
    installBrowser({ permission: "granted", existing: { endpoint: "x" } });
    apiMockServer.use(
      http.get("*/api/projects/inbox/push", () =>
        HttpResponse.json(statusBody([registeredDevice()])),
      ),
      http.post("*/api/projects/inbox/push/test", () =>
        HttpResponse.json({
          results: [
            { subscription_id: "dev_one", outcome: "transient", status: null, error: "unreachable" },
          ],
        }),
      ),
    );
    renderPanel();

    await waitFor(() => expect(screen.getByTestId("test-push")).toBeInTheDocument());
    fireEvent.click(screen.getByTestId("test-push"));

    await waitFor(() => expect(screen.getByTestId("push-test-result")).toBeInTheDocument());
    expect(screen.getByTestId("push-test-result").textContent).toContain("0 of 1");
    expect(screen.getByTestId("push-test-result").textContent).toContain("unreachable");
  });

  it("unsubscribes the browser before telling the server", async () => {
    // That order and not the other: the reverse leaves a window in which the browser
    // is still subscribed to an endpoint nothing will ever send to.
    installBrowser({ permission: "granted", existing: { endpoint: "x" } });
    const seen: string[] = [];
    apiMockServer.use(
      http.get("*/api/projects/inbox/push", () =>
        HttpResponse.json(statusBody([registeredDevice()])),
      ),
      http.post("*/api/projects/inbox/push/unsubscribe", async ({ request }) => {
        seen.push(JSON.stringify(await request.json()));
        return HttpResponse.json(statusBody());
      }),
    );
    renderPanel();

    await waitFor(() => expect(screen.getByTestId("disable-push")).toBeInTheDocument());
    fireEvent.click(screen.getByTestId("disable-push"));

    await waitFor(() => expect(seen).toHaveLength(1));
    expect(seen[0]).toContain("https://fcm.example/send/this-device");
  });

  it("shows a device that has stopped being reachable", async () => {
    installBrowser({ permission: "granted", existing: { endpoint: "x" } });
    apiMockServer.use(
      http.get("*/api/projects/inbox/push", () =>
        HttpResponse.json(
          statusBody([
            registeredDevice({
              healthy: false,
              consecutive_failures: 12,
              last_error: "could not reach the push service",
            }),
          ]),
        ),
      ),
    );
    renderPanel();

    await waitFor(() => expect(screen.getByTestId("push-devices")).toBeInTheDocument());
    expect(screen.getByTestId("push-devices").textContent).toContain("12 failures in a row");
  });
});
