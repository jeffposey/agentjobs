import type { SpecDraftResponse } from "../api/generated";

/**
 * Putting a model's draft into a form without destroying what a person typed.
 *
 * This module is small and has no React in it because it is the part of task-175 that
 * has to be *right* rather than merely to work. The constraint it exists for:
 *
 * > It never overwrites text the human wrote without that being visible and reversible.
 * > Losing the one true sentence someone dictated, inside a wall of generated prose, is
 * > the failure mode that would kill this feature.
 *
 * So `applySpecDraft` is a pure function from (what is in the form, what came back) to
 * (what the form should now hold, what was replaced, what was filled). The banner reads
 * `replaced`, the undo restores the values that went in, and both are testable without
 * rendering anything.
 */

/** The form fields a draft may write. There is no entry here for state a model does not own. */
export const DRAFTABLE_FIELDS = [
  "summary",
  "intent",
  "description",
  "constraints",
  "out_of_scope",
  "acceptance",
] as const;

export type DraftableField = (typeof DRAFTABLE_FIELDS)[number];

/**
 * What a form holds for each draftable field, as the string its input contains.
 *
 * `acceptance` is a newline-joined string rather than an array because that is what the
 * one-per-line textarea holds, and the whole point of this module is to compare against
 * and restore *what the input actually had in it*. Converting to an array here would
 * mean the undo restored a normalisation of the person's text rather than their text.
 */
export type DraftableValues = Record<DraftableField, string>;

/** Human-readable names, for a banner that has to say which fields were touched. */
export const FIELD_LABELS: Record<DraftableField, string> = {
  summary: "Summary",
  intent: "Intent",
  description: "Description",
  constraints: "Constraints",
  out_of_scope: "Out of scope",
  acceptance: "Acceptance criteria",
};

export type AppliedDraft = {
  /** What every draftable field should now contain. */
  next: DraftableValues;
  /** What it contained before, so an undo can put it back exactly. */
  previous: DraftableValues;
  /** Fields that had text of the person's and now hold the model's instead. */
  replaced: Array<DraftableField>;
  /** Fields that were empty and now hold something. */
  filled: Array<DraftableField>;
};

export const EMPTY_VALUES: DraftableValues = {
  summary: "",
  intent: "",
  description: "",
  constraints: "",
  out_of_scope: "",
  acceptance: "",
};

function draftValue(draft: SpecDraftResponse, field: DraftableField): string {
  if (field === "acceptance") return (draft.acceptance ?? []).join("\n");
  const value = draft[field];
  return typeof value === "string" ? value : "";
}

/**
 * Work out what the form should hold after a draft arrives.
 *
 * Two rules, and the second is the one that matters.
 *
 * **A field the draft left empty is not touched.** A model that returned nothing for
 * `constraints` has not decided the person's constraints should be blank; it has
 * declined to write any. Clearing the field would be the model deleting text on the
 * strength of having nothing to say.
 *
 * **A field that already had the person's text is recorded as `replaced`.** That list is
 * what the banner names and what makes the overwrite *visible*; `previous` is what makes
 * it *reversible*. Neither is advisory -- with an empty `replaced` list the banner has
 * nothing to warn about, which is exactly the case where nothing was overwritten.
 */
export function applySpecDraft(
  current: DraftableValues,
  draft: SpecDraftResponse,
): AppliedDraft {
  const next: DraftableValues = { ...current };
  const replaced: Array<DraftableField> = [];
  const filled: Array<DraftableField> = [];

  for (const field of DRAFTABLE_FIELDS) {
    const incoming = draftValue(draft, field).trim();
    if (!incoming) continue;
    const existing = (current[field] ?? "").trim();
    if (existing === incoming) continue;
    next[field] = draftValue(draft, field);
    if (existing) replaced.push(field);
    else filled.push(field);
  }

  return { next, previous: { ...current }, replaced, filled };
}

/** Whether a draft changed anything at all, so a no-op can say so rather than claim success. */
export function changedAnything(applied: AppliedDraft): boolean {
  return applied.replaced.length > 0 || applied.filled.length > 0;
}

/** "Summary, Intent and Acceptance criteria" -- for one readable sentence in a banner. */
export function nameFields(fields: Array<DraftableField>): string {
  const labels = fields.map((field) => FIELD_LABELS[field]);
  if (labels.length <= 1) return labels[0] ?? "";
  return `${labels.slice(0, -1).join(", ")} and ${labels[labels.length - 1]}`;
}
