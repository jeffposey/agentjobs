import { describe, expect, it } from "vitest";

import type { PendingAttachment } from "./attachments";
import {
  dehydrate,
  draftHasContent,
  hydrate,
  memoryDraftStore,
  type CaptureDraft,
} from "./draftStore";

/**
 * What can be checked about the draft store without a browser.
 *
 * The round trip through real IndexedDB is checked where it is real -- `capture-draft`
 * in the Playwright suite reloads the page mid-sentence and expects the sentence back,
 * which is the only instrument that can tell whether a reload keeps it. jsdom has no
 * IndexedDB at all, so a unit test of that half would be a test of a shim.
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

/** A pristine form: opened, nothing typed, its own defaults in place. */
function empty(): CaptureDraft {
  return {
    title: "",
    details: "",
    attachments: [],
    destination: "agentjobs",
    actionable: false,
    priority: "medium",
    expanded: false,
    wantsDraft: true,
    fields: {
      summary: "",
      intent: "",
      constraints: "",
      out_of_scope: "",
      context: "",
      acceptance: "",
      id: "",
      parent: "",
      category: "general",
      effort: "",
      tags: "reported-issue",
      dependencies: "",
    },
  };
}

describe("dehydrate and hydrate", () => {
  it("round-trips a draft with its screenshots", () => {
    const original: CaptureDraft = {
      ...empty(),
      title: "The filters match nothing",
      details: "Every filter returns zero rows.",
      attachments: [image("shot"), image("other")],
      expanded: true,
    };
    expect(hydrate(dehydrate("capture", original))).toEqual(original);
  });

  it("stores each screenshot's bytes exactly once", () => {
    // The reason `preview` is dropped: it is `data:<type>;base64,<bytes>` and the bytes
    // are already in the record beside it, so keeping both doubles a draft holding a
    // screenshot on disk to save one string concatenation.
    const stored = JSON.stringify(dehydrate("capture", { ...empty(), attachments: [image("a")] }));
    expect(stored.split(BYTES).length - 1).toBe(1);
    expect(stored).not.toContain("data:image");
  });

  it("keys the record, and does not leave the key in what comes back", () => {
    const stored = dehydrate("capture", empty());
    expect(stored.id).toBe("capture");
    expect(hydrate(stored)).not.toHaveProperty("id");
  });

  it("rebuilds the preview from the stored media type, not from a guess", () => {
    const jpeg = { ...image("shot"), mediaType: "image/jpeg" };
    const back = hydrate(dehydrate("capture", { ...empty(), attachments: [jpeg] }));
    expect(back.attachments[0]?.preview).toBe(`data:image/jpeg;base64,${BYTES}`);
  });
});

describe("draftHasContent", () => {
  it("says no to a form that was opened and not typed in", () => {
    // The whole point of the exception: `category` and `tags` arrive prefilled, and a
    // draft written for somebody who typed nothing would restore an identical empty
    // form -- while also telling the service worker this tab may never reload.
    expect(draftHasContent(empty())).toBe(false);
  });

  it("says yes to a title, a note, or a screenshot on its own", () => {
    expect(draftHasContent({ ...empty(), title: "Half a thought" })).toBe(true);
    expect(draftHasContent({ ...empty(), details: "Half a thought" })).toBe(true);
    expect(draftHasContent({ ...empty(), attachments: [image("shot")] })).toBe(true);
  });

  it("says no to whitespace, which is not work anybody would miss", () => {
    expect(draftHasContent({ ...empty(), title: "   ", details: "\n" })).toBe(false);
  });

  it("says yes to a specification field, which is the half nobody sees", () => {
    const withSpec = { ...empty(), fields: { ...empty().fields, acceptance: "It works." } };
    expect(draftHasContent(withSpec)).toBe(true);
  });

  it("says yes once a prefilled field is changed away from its default", () => {
    const retagged = { ...empty(), fields: { ...empty().fields, tags: "reported-issue, pwa" } };
    expect(draftHasContent(retagged)).toBe(true);
    const recategorised = { ...empty(), fields: { ...empty().fields, category: "frontend" } };
    expect(draftHasContent(recategorised)).toBe(true);
  });

  it("says yes to a field this file has never heard of", () => {
    // Default-deny on the unknown: a field added to the form and forgotten here reads
    // as content, so the failure is a draft kept too eagerly rather than text lost.
    expect(draftHasContent({ ...empty(), fields: { ...empty().fields, invented: "x" } })).toBe(
      true,
    );
  });
});

describe("memoryDraftStore", () => {
  it("says it is not durable, which is what a browser without IndexedDB gets", () => {
    expect(memoryDraftStore().durable).toBe(false);
  });

  it("gives back what was saved, and nothing for a key never written", async () => {
    const store = memoryDraftStore();
    const draft = { ...empty(), title: "Kept" };
    await store.save("capture", draft);
    expect(await store.load("capture")).toEqual(draft);
    expect(await store.load("somewhere-else")).toBeNull();
  });

  it("replaces the draft written again under the same key", async () => {
    const store = memoryDraftStore({ capture: { ...empty(), title: "First" } });
    await store.save("capture", { ...empty(), title: "Second" });
    expect((await store.load("capture"))?.title).toBe("Second");
  });

  it("forgets a cleared draft, which is what a collect does to it", async () => {
    const store = memoryDraftStore({ capture: { ...empty(), title: "Collected" } });
    await store.clear("capture");
    expect(await store.load("capture")).toBeNull();
  });
});
