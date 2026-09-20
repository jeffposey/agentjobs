import { describe, expect, it } from "vitest";

import type { PendingAttachment } from "./attachments";
import {
  batchSummary,
  errorFor,
  inCollectedOrder,
  nextOrder,
  submitLabel,
  trayRequest,
  type TrayFiling,
  type TrayItem,
} from "./tray";

function image(id: string, bytes: string): PendingAttachment {
  return {
    id,
    label: `${id}.png`,
    mediaType: "image/png",
    sizeBytes: bytes.length,
    dataBase64: bytes,
    preview: `data:image/png;base64,${bytes}`,
  };
}

function item(overrides: Partial<TrayItem> = {}): TrayItem {
  return {
    id: "item-1",
    order: 1,
    projectId: "agentjobs",
    route: "/p/agentjobs/tasks",
    attachments: [],
    request: {
      title: "The filters match nothing",
      description: "Every filter returns an empty list.",
      lifecycle: "draft",
      tags: ["reported-issue"],
      actor: "Jeff Posey",
      operation_id: "11111111-1111-4111-8111-111111111111",
      attachments: [],
      dependencies: [],
    },
    ...overrides,
  };
}

function filing(overrides: Partial<TrayFiling> = {}): TrayFiling {
  return {
    itemId: "item-1",
    title: "The filters match nothing",
    projectId: "agentjobs",
    taskId: "task-501-filters",
    error: null,
    ...overrides,
  };
}

describe("nextOrder", () => {
  it("starts at one and lands a new item after everything held", () => {
    expect(nextOrder([])).toBe(1);
    expect(nextOrder([item({ order: 1 }), item({ id: "item-2", order: 7 })])).toBe(8);
  });

  it("is derived from the items rather than from a clock", () => {
    // Two findings collected in the same millisecond must not tie, which is what a
    // `Date.now()` order would do -- and a tie is a list that reorders itself between
    // one render and the next.
    const first = item({ id: "a", order: nextOrder([]) });
    const second = item({ id: "b", order: nextOrder([first]) });
    expect(second.order).toBeGreaterThan(first.order);
  });
});

describe("inCollectedOrder", () => {
  it("shows the tray in the order it was collected in, not in key order", () => {
    // The store is keyed by a UUID, so what comes back out of it is in no useful order.
    const shuffled = [
      item({ id: "c", order: 3 }),
      item({ id: "a", order: 1 }),
      item({ id: "b", order: 2 }),
    ];
    expect(inCollectedOrder(shuffled).map((entry) => entry.id)).toEqual(["a", "b", "c"]);
  });

  it("leaves a tie in array order, which is how a load merges ahead of a fast collect", () => {
    const tied = [item({ id: "stored", order: 1 }), item({ id: "typed", order: 1 })];
    expect(inCollectedOrder(tied).map((entry) => entry.id)).toEqual(["stored", "typed"]);
  });

  it("does not mutate what it is given", () => {
    const held = [item({ id: "b", order: 2 }), item({ id: "a", order: 1 })];
    inCollectedOrder(held);
    expect(held.map((entry) => entry.id)).toEqual(["b", "a"]);
  });
});

describe("trayRequest", () => {
  it("joins the images back on and changes nothing else", () => {
    const collected = item({ attachments: [image("shot", "AAAA"), image("other", "BBBB")] });
    const request = trayRequest(collected);
    expect(request.attachments).toEqual([
      { data_base64: "AAAA", label: "shot.png" },
      { data_base64: "BBBB", label: "other.png" },
    ]);
    // Everything the builder baked in at collect time survives to the wire untouched --
    // the provenance prose, the tag, who filed it.
    expect(request.description).toBe(collected.request.description);
    expect(request.tags).toEqual(["reported-issue"]);
    expect(request.actor).toBe("Jeff Posey");
  });

  it("sends the operation_id the item was collected with, every time", () => {
    // The point of storing it rather than minting one per attempt: a second send of the
    // same item resolves to the task the first attempt made, even when the first
    // attempt's answer never arrived.
    const collected = item();
    expect(trayRequest(collected).operation_id).toBe(
      "11111111-1111-4111-8111-111111111111",
    );
    expect(trayRequest(collected).operation_id).toBe(trayRequest(collected).operation_id);
  });
});

describe("errorFor", () => {
  it("attaches the last batch's error to the card that earned it", () => {
    const batch = [
      filing({ itemId: "a", taskId: null, error: "Unknown actor." }),
      filing({ itemId: "b" }),
    ];
    expect(errorFor("a", batch)).toBe("Unknown actor.");
    expect(errorFor("b", batch)).toBeNull();
    expect(errorFor("never-sent", batch)).toBeNull();
  });
});

describe("submitLabel", () => {
  it("names the count, because that is what to be sure about before pressing it", () => {
    expect(submitLabel(1)).toBe("Create 1 task");
    expect(submitLabel(12)).toBe("Create 12 tasks");
  });
});

describe("batchSummary", () => {
  it("says nothing before a batch has run", () => {
    expect(batchSummary([])).toBe("");
  });

  it("counts a clean batch", () => {
    expect(batchSummary([filing({ itemId: "a" })])).toBe("1 task created.");
    expect(batchSummary([filing({ itemId: "a" }), filing({ itemId: "b" })])).toBe(
      "2 tasks created.",
    );
  });

  it("states a partial success as a partial success, and says the rest is still here", () => {
    const summary = batchSummary([
      filing({ itemId: "a" }),
      filing({ itemId: "b" }),
      filing({ itemId: "c", taskId: null, error: "The server refused it." }),
    ]);
    expect(summary).toContain("2 of 3 created");
    expect(summary).toContain("still here");
    expect(summary).toContain("press the button again");
  });

  it("does not claim a creation when the whole batch failed", () => {
    const summary = batchSummary([
      filing({ itemId: "a", taskId: null, error: "Offline." }),
      filing({ itemId: "b", taskId: null, error: "Offline." }),
    ]);
    expect(summary).toContain("Nothing was created");
    expect(summary).toContain("All 2 findings are still here");
  });

  it("counts one batch, not the session, so a retry does not inherit earlier wins", () => {
    // The retry of a single failed card sent one thing and created one thing. Counting
    // the five the batch before it created would report "6 of 6" about a press that
    // touched one card.
    expect(batchSummary([filing({ itemId: "c" })])).toBe("1 task created.");
  });
});
