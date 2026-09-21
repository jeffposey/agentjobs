import { describe, expect, it } from "vitest";

import type { PendingAttachment } from "./attachments";
import type { TrayItem } from "./tray";
import { dehydrate, hydrate, memoryTrayStore } from "./trayStore";

/**
 * What can be checked about the tray store without a browser.
 *
 * The round trip through real IndexedDB is checked where it is real -- `capture-tray`
 * in the Playwright suite reloads the page and expects the list back, which is the only
 * instrument that can tell whether a reload keeps it. jsdom has no IndexedDB at all, so
 * a unit test of that half would be a test of a shim.
 */

const BYTES = "iVBORw0KGgoAAAANSUhEUg";

function image(id: string): PendingAttachment {
  return {
    id,
    label: `${id}.png`,
    mediaType: "image/png",
    sizeBytes: 22,
    dataBase64: BYTES,
    preview: `data:image/png;base64,${BYTES}`,
  };
}

function item(id: string, order: number, attachments: Array<PendingAttachment> = []): TrayItem {
  return {
    id,
    order,
    projectId: "agentjobs",
    route: "/p/agentjobs/tasks",
    attachments,
    draft: { state: "applied", model: "a-model", filled: ["summary"] },
    request: {
      title: `Finding ${id}`,
      description: "Something was wrong.",
      lifecycle: "draft",
      operation_id: "11111111-1111-4111-8111-111111111111",
      attachments: [],
      dependencies: [],
    },
  };
}

describe("dehydrate and hydrate", () => {
  it("round-trips an item with its screenshots", () => {
    const original = item("a", 1, [image("shot"), image("other")]);
    expect(hydrate(dehydrate(original))).toEqual(original);
  });

  it("stores each screenshot's bytes exactly once", () => {
    // The reason `preview` is dropped: it is `data:<type>;base64,<bytes>` and the bytes
    // are already in the record beside it, so keeping both doubles a tray of screenshots
    // on disk to save one string concatenation.
    const stored = JSON.stringify(dehydrate(item("a", 1, [image("shot")])));
    expect(stored.split(BYTES).length - 1).toBe(1);
    expect(stored).not.toContain("data:image");
  });

  it("rebuilds the preview from the stored media type, not from a guess", () => {
    const jpeg = { ...image("shot"), mediaType: "image/jpeg" };
    const back = hydrate(dehydrate(item("a", 1, [jpeg])));
    expect(back.attachments[0]?.preview).toBe(`data:image/jpeg;base64,${BYTES}`);
  });
});

describe("memoryTrayStore", () => {
  it("says it is not durable, which is what the tray tells the person", () => {
    expect(memoryTrayStore().durable).toBe(false);
  });

  it("loads what was put, in collection order", async () => {
    const store = memoryTrayStore();
    await store.put(item("b", 2));
    await store.put(item("a", 1));
    expect((await store.load()).map((entry) => entry.id)).toEqual(["a", "b"]);
  });

  it("replaces an item written again under the same id", async () => {
    const store = memoryTrayStore([item("a", 1)]);
    await store.put({ ...item("a", 1), route: "/p/agentjobs/dashboard" });
    const held = await store.load();
    expect(held).toHaveLength(1);
    expect(held[0]?.route).toBe("/p/agentjobs/dashboard");
  });

  it("removes only the ids it is given", async () => {
    const store = memoryTrayStore([item("a", 1), item("b", 2), item("c", 3)]);
    await store.remove(["b"]);
    expect((await store.load()).map((entry) => entry.id)).toEqual(["a", "c"]);
  });
});
