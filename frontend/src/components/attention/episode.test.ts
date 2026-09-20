import { describe, expect, it } from "vitest";

import type { AttentionResponse } from "../../api/types";
import {
  ACK_PARAM,
  NOTIFIED_STORAGE_KEY,
  deliveryState,
  notificationFor,
  notificationTag,
  readLastNotified,
  shouldNotify,
  waitingPath,
  writeLastNotified,
} from "./episode";

/**
 * What a client does with an attention episode (task-422).
 *
 * Every assertion here is on a value a browser or a person acts on -- the sentence in
 * the notification, the URL it opens, the decision to draw it at all -- rather than on
 * a call having been made. The policy itself is the server's and is tested in
 * `tests/test_attention_episodes.py`; what is under test here is the half that could
 * draw the same alert twice.
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
      lead_ask: "Needs review",
    },
    ...overrides,
  } as AttentionResponse;
}

describe("shouldNotify", () => {
  it("draws the first unacknowledged episode this browser has seen", () => {
    expect(shouldNotify(attention(), null)).toBe(true);
  });

  it("does not draw the same episode twice", () => {
    // The reload case, and the poll case: the header asks every fifteen seconds and a
    // toast per poll is the failure this whole model exists to prevent.
    expect(shouldNotify(attention(), "att_one")).toBe(false);
  });

  it("does not draw an episode the person has already acted on", () => {
    const acknowledged = attention();
    acknowledged.episode!.acknowledged = true;

    expect(shouldNotify(acknowledged, null)).toBe(false);
  });

  it("draws again once a new episode has opened", () => {
    const rearmed = attention();
    rearmed.episode!.id = "att_two";

    expect(shouldNotify(rearmed, "att_one")).toBe(true);
  });

  it("says nothing when nothing is waiting", () => {
    expect(shouldNotify({ blocking: 0, episode: null }, null)).toBe(false);
    expect(shouldNotify(null, null)).toBe(false);
  });
});

describe("notificationFor", () => {
  /**
   * task-421: the body used to name the task and stop there. A task title says which
   * work is stopped and never what is wanted of you, so the one decision a person makes
   * from a lock screen -- go and find a computer, or not -- was the one it did not help
   * with. The ask leads for that reason.
   */
  it("leads with what is being asked, then names the task", () => {
    const note = notificationFor(attention(), "agentjobs");

    expect(note?.title).toBe("1 task is waiting on you");
    expect(note?.body).toBe("Needs review — task-001: Review the branch");
  });

  it("keeps the ask in front when several are waiting", () => {
    const several = attention({ blocking: 4 });
    several.episode!.tasks = ["task-001", "task-002", "task-003", "task-004"];

    const note = notificationFor(several, "agentjobs");

    expect(note?.title).toBe("4 tasks are waiting on you");
    expect(note?.body).toBe("Needs review — task-001: Review the branch — and 3 others.");
  });

  it("falls back to naming the task where the server sends no ask", () => {
    const older = attention();
    delete (older.episode as { lead_ask?: string }).lead_ask;

    const note = notificationFor(older, "agentjobs");

    expect(note?.body).toBe("task-001: Review the branch");
  });

  it("gets the grammar right for exactly two", () => {
    const two = attention({ blocking: 2 });
    two.episode!.tasks = ["task-001", "task-002"];

    expect(notificationFor(two, "agentjobs")?.body).toBe(
      "Needs review — task-001: Review the branch — and 1 other.",
    );
  });

  it("carries one tag per project, so the shell replaces rather than stacks", () => {
    expect(notificationFor(attention(), "agentjobs")?.tag).toBe(notificationTag("agentjobs"));
  });

  it("is nothing at all when nothing is waiting", () => {
    expect(notificationFor({ blocking: 0, episode: null }, "agentjobs")).toBeNull();
  });
});

describe("waitingPath", () => {
  const episode = { id: "att_one", tasks: ["task-001"], lead_task_id: "task-001" };

  it("opens the task itself when there is only one", () => {
    expect(waitingPath("agentjobs", episode)).toBe(
      `/app/p/agentjobs/tasks/task-001?${ACK_PARAM}=att_one`,
    );
  });

  it("opens the filtered waiting list when there are several", () => {
    const many = { ...episode, tasks: ["task-001", "task-002"] };

    expect(waitingPath("agentjobs", many)).toBe(
      `/app/p/agentjobs/tasks?status=human&${ACK_PARAM}=att_one`,
    );
  });

  it("escapes a project id that would otherwise change the path", () => {
    expect(waitingPath("a b", episode)).toContain("/app/p/a%20b/");
  });
});

describe("the last-notified marker", () => {
  it("round-trips through storage", () => {
    const store = new Map<string, string>();
    const fake = {
      getItem: (key: string) => store.get(key) ?? null,
      setItem: (key: string, value: string) => void store.set(key, value),
      removeItem: (key: string) => void store.delete(key),
    };

    writeLastNotified("att_one", fake);

    expect(store.get(NOTIFIED_STORAGE_KEY)).toBe("att_one");
    expect(readLastNotified(fake)).toBe("att_one");
  });

  it("survives a storage that throws, at the cost of one extra notification", () => {
    // A private window, blocked site data, or a browser that throws on the getter. The
    // notifier must keep working; the consequence is a repeated alert, not a crash.
    const hostile = {
      getItem: () => {
        throw new Error("blocked");
      },
      setItem: () => {
        throw new Error("blocked");
      },
      removeItem: () => {},
    };

    expect(readLastNotified(hostile)).toBeNull();
    expect(() => writeLastNotified("att_one", hostile)).not.toThrow();
  });
});

describe("deliveryState", () => {
  it("tells a browser that cannot apart from a permission that has not been asked", () => {
    expect(deliveryState(undefined)).toBe("unsupported");
    expect(deliveryState({ permission: "default" })).toBe("askable");
  });

  it("tells a refusal apart from a grant", () => {
    expect(deliveryState({ permission: "denied" })).toBe("denied");
    expect(deliveryState({ permission: "granted" })).toBe("granted");
  });
});
