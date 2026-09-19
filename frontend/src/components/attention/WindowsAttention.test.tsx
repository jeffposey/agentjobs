import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { AttentionResponse } from "../../api/types";
import { NOTIFIED_STORAGE_KEY } from "./episode";

const applyAppBadge = vi.fn().mockResolvedValue("set");
const paintFavicon = vi.fn();
const deliver = vi.fn().mockResolvedValue("shown");

vi.mock("./shell", async () => {
  const actual = await vi.importActual<typeof import("./shell")>("./shell");
  return {
    ...actual,
    applyAppBadge: (...args: unknown[]) => applyAppBadge(...args),
    paintFavicon: (...args: unknown[]) => paintFavicon(...args),
    deliver: (...args: unknown[]) => deliver(...args),
  };
});

const {
  AttentionNotifier,
  NotificationDelivery,
  useAcknowledgeFromUrl,
  useAcknowledgeOnOpen,
} = await import("./WindowsAttention");

/**
 * The notifier, asserted on what it hands the shell (task-422).
 *
 * `./shell` is mocked because the questions here are *how often* and *with what* --
 * one notification per episode, the count on the badge, nothing at all when the
 * episode is acknowledged. What each shell call does with its argument is
 * `shell.test.ts`, and a jsdom without `Notification` would answer every one of these
 * questions "unsupported" if the real module were used.
 */

function attention(overrides: Partial<AttentionResponse> = {}): AttentionResponse {
  return {
    blocking: 1,
    episode: {
      id: "att_one",
      started_at: "2026-09-19T12:00:00Z",
      acknowledged: false,
      tasks: ["task-001"],
      lead_task_id: "task-001",
      lead_task_title: "Review the branch",
    },
    ...overrides,
  } as AttentionResponse;
}

beforeEach(() => {
  applyAppBadge.mockClear();
  paintFavicon.mockClear();
  deliver.mockClear();
  window.localStorage.removeItem(NOTIFIED_STORAGE_KEY);
});

afterEach(() => {
  window.localStorage.removeItem(NOTIFIED_STORAGE_KEY);
});

describe("the persistent indicator", () => {
  it("puts the count on the taskbar and the tab", async () => {
    render(<AttentionNotifier projectId="agentjobs" attention={attention({ blocking: 3 })} />);

    await waitFor(() => expect(applyAppBadge).toHaveBeenCalledWith(3));
    expect(paintFavicon).toHaveBeenCalledWith(3);
  });

  it("stays up after the person has acknowledged the episode", async () => {
    // The rule this asserts: acknowledgment governs interruption, not the indicator.
    // Work is still stopped on you after you have looked at it.
    const acknowledged = attention({ blocking: 2 });
    acknowledged.episode!.acknowledged = true;

    render(<AttentionNotifier projectId="agentjobs" attention={acknowledged} />);

    await waitFor(() => expect(applyAppBadge).toHaveBeenCalledWith(2));
    expect(deliver).not.toHaveBeenCalled();
  });

  it("clears when the waiting set empties", async () => {
    render(
      <AttentionNotifier projectId="agentjobs" attention={{ blocking: 0, episode: null }} />,
    );

    await waitFor(() => expect(applyAppBadge).toHaveBeenCalledWith(0));
    expect(paintFavicon).toHaveBeenCalledWith(0);
  });

  it("takes the number off the taskbar when the shell unmounts", async () => {
    const { unmount } = render(
      <AttentionNotifier projectId="agentjobs" attention={attention()} />,
    );

    unmount();

    await waitFor(() => expect(applyAppBadge).toHaveBeenLastCalledWith(0));
  });
});

describe("the notification", () => {
  it("is raised once for a new episode", async () => {
    render(<AttentionNotifier projectId="agentjobs" attention={attention()} />);

    await waitFor(() => expect(deliver).toHaveBeenCalledTimes(1));
    expect(deliver.mock.calls[0]?.[0]).toMatchObject({
      episodeId: "att_one",
      title: "1 task is waiting on you",
    });
  });

  it("is not raised again when a task joins the same unacknowledged episode", async () => {
    const { rerender } = render(
      <AttentionNotifier projectId="agentjobs" attention={attention()} />,
    );
    await waitFor(() => expect(deliver).toHaveBeenCalledTimes(1));

    const joined = attention({ blocking: 2 });
    joined.episode!.tasks = ["task-001", "task-002"];
    rerender(<AttentionNotifier projectId="agentjobs" attention={joined} />);

    await waitFor(() => expect(applyAppBadge).toHaveBeenCalledWith(2));
    expect(deliver).toHaveBeenCalledTimes(1);
  });

  it("is raised again once the episode has been acknowledged and re-armed", async () => {
    const { rerender } = render(
      <AttentionNotifier projectId="agentjobs" attention={attention()} />,
    );
    await waitFor(() => expect(deliver).toHaveBeenCalledTimes(1));

    const rearmed = attention({ blocking: 2 });
    rearmed.episode!.id = "att_two";
    rerender(<AttentionNotifier projectId="agentjobs" attention={rearmed} />);

    await waitFor(() => expect(deliver).toHaveBeenCalledTimes(2));
  });

  it("is not replayed on a cold start of an episode this browser already drew", async () => {
    // The browser-restart case. The episode is still open and still unacknowledged;
    // what stops the alert is this client's own record of having shown it.
    window.localStorage.setItem(NOTIFIED_STORAGE_KEY, "att_one");

    render(<AttentionNotifier projectId="agentjobs" attention={attention()} />);

    await waitFor(() => expect(applyAppBadge).toHaveBeenCalled());
    expect(deliver).not.toHaveBeenCalled();
  });

  it("raises one summary rather than one per task when several are already waiting", async () => {
    // The storm case: a client opening onto five existing waits.
    const many = attention({ blocking: 5 });
    many.episode!.tasks = ["task-001", "task-002", "task-003", "task-004", "task-005"];

    render(<AttentionNotifier projectId="agentjobs" attention={many} />);

    await waitFor(() => expect(deliver).toHaveBeenCalledTimes(1));
    expect(deliver.mock.calls[0]?.[0]?.title).toBe("5 tasks are waiting on you");
  });

  it("does not retry an episode whose delivery failed", async () => {
    // A denied permission or a dead worker must not mean a fresh attempt on every
    // fifteen-second poll for the rest of the day. The episode is still red on screen.
    deliver.mockResolvedValueOnce("blocked");
    const { rerender } = render(
      <AttentionNotifier projectId="agentjobs" attention={attention()} />,
    );
    await waitFor(() => expect(deliver).toHaveBeenCalledTimes(1));

    rerender(<AttentionNotifier projectId="agentjobs" attention={attention({ blocking: 1 })} />);

    await waitFor(() => expect(paintFavicon).toHaveBeenCalled());
    expect(deliver).toHaveBeenCalledTimes(1);
  });
});

function AckFromUrlHarness({ onAcknowledge }: { onAcknowledge: (id: string) => void }) {
  useAcknowledgeFromUrl(onAcknowledge);
  const location = useLocation();
  return <output data-testid="url">{`${location.pathname}${location.search}`}</output>;
}

describe("acknowledging from a notification click", () => {
  it("acknowledges the episode the click carried", async () => {
    const acknowledge = vi.fn();

    render(
      <MemoryRouter initialEntries={["/p/agentjobs/tasks/task-001?attention_ack=att_one"]}>
        <Routes>
          <Route
            path="/p/:projectId/tasks/:taskId"
            element={<AckFromUrlHarness onAcknowledge={acknowledge} />}
          />
        </Routes>
      </MemoryRouter>,
    );

    await waitFor(() => expect(acknowledge).toHaveBeenCalledWith("att_one"));
  });

  it("takes the marker off the URL, so a reload does not acknowledge again", async () => {
    const acknowledge = vi.fn();

    render(
      <MemoryRouter initialEntries={["/p/agentjobs/tasks?status=human&attention_ack=att_one"]}>
        <Routes>
          <Route path="/p/:projectId/tasks" element={<AckFromUrlHarness onAcknowledge={acknowledge} />} />
        </Routes>
      </MemoryRouter>,
    );

    await waitFor(() =>
      expect(screen.getByTestId("url")).toHaveTextContent("/p/agentjobs/tasks?status=human"),
    );
    expect(screen.getByTestId("url").textContent).not.toContain("attention_ack");
  });

  it("does nothing on an ordinary URL", async () => {
    const acknowledge = vi.fn();

    render(
      <MemoryRouter initialEntries={["/p/agentjobs/tasks"]}>
        <Routes>
          <Route path="/p/:projectId/tasks" element={<AckFromUrlHarness onAcknowledge={acknowledge} />} />
        </Routes>
      </MemoryRouter>,
    );

    await waitFor(() => expect(screen.getByTestId("url")).toBeInTheDocument());
    expect(acknowledge).not.toHaveBeenCalled();
  });
});

function OpenTaskHarness({
  taskId,
  onAcknowledge,
}: {
  taskId: string | null;
  onAcknowledge: (id: string) => void;
}) {
  useAcknowledgeOnOpen(attention(), taskId, onAcknowledge);
  return null;
}

describe("acknowledging by opening a task the episode names", () => {
  it("acknowledges when the open task is in the episode", async () => {
    const acknowledge = vi.fn();

    render(<OpenTaskHarness taskId="task-001" onAcknowledge={acknowledge} />);

    await waitFor(() => expect(acknowledge).toHaveBeenCalledWith("att_one"));
  });

  it("does not acknowledge for a task the episode does not name", async () => {
    // Reading the backlog is not being told. The gesture that acknowledges has to be
    // one aimed at the work that stopped.
    const acknowledge = vi.fn();

    render(<OpenTaskHarness taskId="task-999" onAcknowledge={acknowledge} />);

    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(acknowledge).not.toHaveBeenCalled();
  });

  it("does not acknowledge on a surface with no task open", async () => {
    const acknowledge = vi.fn();

    render(<OpenTaskHarness taskId={null} onAcknowledge={acknowledge} />);

    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(acknowledge).not.toHaveBeenCalled();
  });
});

/**
 * jsdom has no `Notification`, so the permission has to be installed for these.
 * Written as a real object rather than a spy on a global, because `deliveryState`
 * reads `permission` at render time and a getter is what a browser actually exposes.
 */
function withNotificationPermission(permission: NotificationPermission | null) {
  const original = Object.getOwnPropertyDescriptor(window, "Notification");
  if (permission === null) {
    // @ts-expect-error - modelling a browser that has no Notification at all
    delete window.Notification;
    return () => {
      if (original) Object.defineProperty(window, "Notification", original);
    };
  }
  Object.defineProperty(window, "Notification", {
    configurable: true,
    writable: true,
    value: Object.assign(function () {}, {
      permission,
      requestPermission: () => Promise.resolve("granted" as NotificationPermission),
    }),
  });
  return () => {
    if (original) Object.defineProperty(window, "Notification", original);
    else {
      // @ts-expect-error - restoring a global that did not exist
      delete window.Notification;
    }
  };
}

describe("the degraded state", () => {
  it("says nothing at all once permission is granted", () => {
    const restore = withNotificationPermission("granted");
    try {
      const { container } = render(<NotificationDelivery />);
      expect(container).toBeEmptyDOMElement();
    } finally {
      restore();
    }
  });

  it("offers to ask when permission has not been decided", async () => {
    const restore = withNotificationPermission("default");
    try {
      render(<NotificationDelivery />);
      expect(screen.getByTestId("attention-delivery")).toHaveAttribute(
        "data-delivery",
        "askable",
      );
      // The request must happen inside the click: Chrome refuses a prompt that is not
      // a response to a gesture, and a refused prompt looks exactly like a denial.
      fireEvent.click(screen.getByTestId("enable-notifications"));
      await waitFor(() =>
        expect(screen.queryByTestId("attention-delivery")).not.toBeInTheDocument(),
      );
    } finally {
      restore();
    }
  });

  it("says how to undo a refusal, and that the badge still works", () => {
    const restore = withNotificationPermission("denied");
    try {
      render(<NotificationDelivery />);
      const notice = screen.getByTestId("attention-delivery");
      expect(notice).toHaveAttribute("data-delivery", "denied");
      expect(notice).toHaveTextContent(/Site settings/);
      expect(notice).toHaveTextContent(/red badge in the header still tracks/);
    } finally {
      restore();
    }
  });

  it("says something different for a browser that simply cannot", () => {
    const restore = withNotificationPermission(null);
    try {
      render(<NotificationDelivery />);
      const notice = screen.getByTestId("attention-delivery");
      expect(notice).toHaveAttribute("data-delivery", "unsupported");
      expect(notice).toHaveTextContent(/cannot raise desktop notifications/);
    } finally {
      restore();
    }
  });

  /**
   * task-421: this notice and the phone panel both rendered on every device, so a phone
   * was told its Windows notifications were off. The permission state here is the one
   * that would otherwise produce the loudest banner.
   */
  it("says nothing at all on a phone, whatever the permission state", () => {
    const restore = withNotificationPermission("default");
    Object.defineProperty(navigator, "userAgentData", {
      configurable: true,
      value: { mobile: true },
    });
    try {
      const { container } = render(<NotificationDelivery />);
      expect(container).toBeEmptyDOMElement();
    } finally {
      Object.defineProperty(navigator, "userAgentData", {
        configurable: true,
        value: undefined,
      });
      restore();
    }
  });

  it("never names an operating system the person may not be on", () => {
    const restore = withNotificationPermission("denied");
    try {
      render(<NotificationDelivery />);
      expect(screen.getByTestId("attention-delivery")).not.toHaveTextContent(/Windows/);
    } finally {
      restore();
    }
  });
});
