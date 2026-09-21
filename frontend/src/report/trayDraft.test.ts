import { describe, expect, it } from "vitest";

import type { SpecDraftResponse, TaskCreateRequest } from "../api/generated";
import { buildCaptureRequest, insertAboveProvenance } from "./issueReport";
import { mergeDraft, nameFilled } from "./trayDraft";

const NOTE = "At 375px the two dashboard cards sit on top of each other.";

/** A capture as `buildCaptureRequest` really produces it, footer and all. */
function captured(details = NOTE): TaskCreateRequest {
  return buildCaptureRequest({
    draft: { title: "The dashboard cards overlap", details, actionable: false },
    context: { route: "/p/agentjobs", projectId: "agentjobs", taskId: null },
    destinationProjectId: "agentjobs",
    reporter: "Jeff Posey",
    operationId: "11111111-1111-4111-8111-111111111111",
  });
}

function drafted(overrides: Partial<SpecDraftResponse> = {}): SpecDraftResponse {
  return {
    drafted: true,
    summary: "Two dashboard cards overlap below 400px.",
    intent: "A dashboard nobody can read on a phone is a dashboard nobody reads.",
    description: "Give the card grid a single column below the `sm` breakpoint.",
    constraints: "The desktop layout must not change.",
    out_of_scope: "The cards' contents.",
    acceptance: ["At 375px the cards stack", "At 1024px they sit side by side"],
    ...overrides,
  } as SpecDraftResponse;
}

describe("insertAboveProvenance", () => {
  it("puts a block between the note and the footer a capture already carries", () => {
    const description = captured().description ?? "";
    const merged = insertAboveProvenance(description, "INSERTED");
    expect(merged.indexOf(NOTE)).toBeLessThan(merged.indexOf("INSERTED"));
    expect(merged.indexOf("INSERTED")).toBeLessThan(
      merged.indexOf("Reported from the AgentJobs UI"),
    );
    // Nothing of the original is lost or reordered.
    expect(merged.replace("\n\nINSERTED", "")).toBe(description);
  });

  it("appends when there is no footer to sit above", () => {
    expect(insertAboveProvenance("Just a note.", "BLOCK")).toBe("Just a note.\n\nBLOCK");
  });
});

describe("mergeDraft", () => {
  it("fills the fields a quick capture leaves empty", () => {
    const { request, filled } = mergeDraft(captured(), drafted(), "haiku-test");
    expect(request.summary).toBe("Two dashboard cards overlap below 400px.");
    expect(request.intent).toContain("nobody can read on a phone");
    expect(request.constraints).toBe("The desktop layout must not change.");
    expect(request.out_of_scope).toBe("The cards' contents.");
    expect(request.acceptance).toEqual([
      { id: "ac-1", text: "At 375px the cards stack", status: "pending" },
      { id: "ac-2", text: "At 1024px they sit side by side", status: "pending" },
    ]);
    expect(filled).toEqual([
      "summary",
      "intent",
      "constraints",
      "out_of_scope",
      "acceptance",
      "description",
    ]);
  });

  it("never replaces a field the person wrote in", () => {
    // The rule this module exists for. `specDraft.ts` records a replacement so a banner
    // can warn about it; here nobody is looking at a banner, so the overwrite must not
    // happen at all.
    const mine = {
      ...captured(),
      summary: "My own summary, which I mean.",
      constraints: "Mine too.",
      acceptance: [{ id: "ac-1", text: "My own criterion", status: "pending" as const }],
    };
    const { request, filled } = mergeDraft(mine, drafted(), "haiku-test");
    expect(request.summary).toBe("My own summary, which I mean.");
    expect(request.constraints).toBe("Mine too.");
    expect(request.acceptance).toEqual([
      { id: "ac-1", text: "My own criterion", status: "pending" },
    ]);
    // Only what was blank got filled.
    expect(filled).toEqual(["intent", "out_of_scope", "description"]);
  });

  it("keeps the typed note first and verbatim, and attributes what follows it", () => {
    const { request } = mergeDraft(captured(), drafted(), "haiku-test");
    const description = request.description ?? "";
    expect(description).toContain(NOTE);
    expect(description.indexOf(NOTE)).toBe(0);
    expect(description).toContain("Fleshed out below by `haiku-test`, from the note above.");
    expect(description).toContain("Everything above this line is as it was typed.");
    expect(description).toContain("Give the card grid a single column");
    // The footer stays last, which is what makes the record readable three weeks later.
    expect(description.indexOf("Give the card grid")).toBeLessThan(
      description.indexOf("Reported from the AgentJobs UI"),
    );
  });

  it("says a model drafted it even when the machine could not name one", () => {
    const { request } = mergeDraft(captured(), drafted(), null);
    expect(request.description).toContain("Fleshed out below by a model");
  });

  it("leaves a field the model declined to write alone", () => {
    // An empty `constraints` is a model with nothing to say about constraints, not a
    // model deciding there are none.
    const mine = { ...captured(), constraints: "Mine." };
    const { request, filled } = mergeDraft(
      mine,
      drafted({ constraints: "", out_of_scope: "" }),
      "haiku-test",
    );
    expect(request.constraints).toBe("Mine.");
    expect(request.out_of_scope).toBeUndefined();
    expect(filled).not.toContain("out_of_scope");
  });

  it("changes nothing at all when the draft is empty", () => {
    const before = captured();
    const { request, filled } = mergeDraft(
      before,
      drafted({
        summary: "",
        intent: "",
        description: "",
        constraints: "",
        out_of_scope: "",
        acceptance: [],
      }),
      "haiku-test",
    );
    expect(request).toEqual(before);
    expect(filled).toEqual([]);
  });

  it("does not mutate the request it was given", () => {
    const before = captured();
    const description = before.description;
    mergeDraft(before, drafted(), "haiku-test");
    expect(before.summary).toBeUndefined();
    expect(before.description).toBe(description);
  });

  it("cannot set anything that is not prose or a criterion", () => {
    // `modelaccess/draft.py` has no lifecycle, priority, parent, dependency or tag in
    // its output shape; this is the client half of the same guarantee, so a provider
    // that returns them anyway changes nothing.
    const before = captured();
    const { request } = mergeDraft(
      before,
      drafted({
        // Fields the schema does not describe, as a hostile provider might send them.
        ...({ priority: "critical", parent: "task-001", tags: ["urgent"] } as object),
      }),
      "haiku-test",
    );
    expect(request.priority).toBe(before.priority);
    expect(request.parent).toBeUndefined();
    expect(request.tags).toEqual(before.tags);
    expect(request.lifecycle).toBe("draft");
    expect(request.operation_id).toBe(before.operation_id);
  });
});

describe("nameFilled", () => {
  it("reads as a sentence on a card", () => {
    expect(nameFilled(["summary"])).toBe("Summary");
    expect(nameFilled(["summary", "intent", "acceptance"])).toBe(
      "Summary, Intent and Acceptance criteria",
    );
    expect(nameFilled([])).toBe("");
  });
});
