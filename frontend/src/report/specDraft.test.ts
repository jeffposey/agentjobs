import { describe, expect, it } from "vitest";

import type { SpecDraftResponse } from "../api/generated";
import {
  EMPTY_VALUES,
  applySpecDraft,
  changedAnything,
  nameFields,
  type DraftableValues,
} from "./specDraft";

function reply(overrides: Partial<SpecDraftResponse> = {}): SpecDraftResponse {
  return {
    drafted: true,
    summary: "",
    intent: "",
    description: "",
    constraints: "",
    out_of_scope: "",
    acceptance: [],
    model: "a-model-id",
    reason: null,
    detail: null,
    ...overrides,
  };
}

function values(overrides: Partial<DraftableValues> = {}): DraftableValues {
  return { ...EMPTY_VALUES, ...overrides };
}

describe("applySpecDraft", () => {
  it("fills empty fields and reports them as filled, not replaced", () => {
    const applied = applySpecDraft(
      values(),
      reply({ summary: "An orienting sentence.", acceptance: ["One.", "Two."] }),
    );

    expect(applied.next.summary).toBe("An orienting sentence.");
    expect(applied.next.acceptance).toBe("One.\nTwo.");
    expect(applied.filled).toEqual(["summary", "acceptance"]);
    expect(applied.replaced).toEqual([]);
  });

  it("names a field whose text it replaced, which is what makes the overwrite visible", () => {
    // The constraint this whole module exists for: losing the one true sentence
    // somebody dictated, inside a wall of generated prose, is the failure that would
    // kill the feature. The banner reads `replaced`.
    const applied = applySpecDraft(
      values({ summary: "The one true sentence I dictated." }),
      reply({ summary: "A model's longer and blander summary." }),
    );

    expect(applied.replaced).toEqual(["summary"]);
    expect(applied.filled).toEqual([]);
    expect(applied.next.summary).toBe("A model's longer and blander summary.");
  });

  it("keeps the exact text it replaced, so an undo restores it character for character", () => {
    const mine = "  Ragged spacing,\n\nand a blank line I meant.  ";
    const applied = applySpecDraft(
      values({ description: mine }),
      reply({ description: "Tidy generated prose." }),
    );

    expect(applied.previous.description).toBe(mine);
  });

  it("does not clear a field the model declined to write", () => {
    // A model that returned nothing for `constraints` has not decided the person's
    // constraints should be blank -- it has declined to write any.
    const applied = applySpecDraft(
      values({ constraints: "Must not change the schema." }),
      reply({ summary: "Something." }),
    );

    expect(applied.next.constraints).toBe("Must not change the schema.");
    expect(applied.replaced).toEqual([]);
  });

  it("treats an identical value as no change at all", () => {
    const applied = applySpecDraft(
      values({ summary: "The same words." }),
      reply({ summary: "The same words." }),
    );

    expect(changedAnything(applied)).toBe(false);
  });

  it("has nowhere to put state the model does not own", () => {
    // ac-5 on the client side. The response type carries no such field, so this is a
    // deliberately ugly cast: the assertion is that even a server that answered with
    // one could not move it into the form.
    const rogue = {
      ...reply({ summary: "Fine." }),
      priority: "critical",
      parent: "task-001-invented",
      lifecycle: "ready",
      actor: "somebody",
    } as unknown as SpecDraftResponse;

    const applied = applySpecDraft(values(), rogue);

    expect(Object.keys(applied.next).sort()).toEqual([
      "acceptance",
      "constraints",
      "description",
      "intent",
      "out_of_scope",
      "summary",
    ]);
    expect(JSON.stringify(applied.next)).not.toContain("critical");
    expect(JSON.stringify(applied.next)).not.toContain("task-001-invented");
  });
});

describe("nameFields", () => {
  it("reads as a sentence", () => {
    expect(nameFields(["summary"])).toBe("Summary");
    expect(nameFields(["summary", "intent"])).toBe("Summary and Intent");
    expect(nameFields(["summary", "intent", "acceptance"])).toBe(
      "Summary, Intent and Acceptance criteria",
    );
  });
});
