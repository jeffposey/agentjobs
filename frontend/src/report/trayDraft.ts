import type { SpecDraftResponse, TaskCreateRequest } from "../api/generated";
import { insertAboveProvenance } from "./issueReport";
import { FIELD_LABELS, type DraftableField } from "./specDraft";

/**
 * Fleshing out a collected finding, with nobody watching (task-121).
 *
 * **This is a different merge from `specDraft.ts`, on purpose.** That one exists for the
 * single-capture form, where a draft lands in inputs a person then reads: it *records* a
 * replacement so a banner can warn about it and an undo can put the original back. On
 * this path there is no banner and no undo -- a finding is typed, Ctrl+Enter, and the
 * next one is already being typed -- so the same rule would mean the model quietly
 * overwriting somebody's own sentence in a record nobody looked at before it was filed.
 *
 * So the rule here is **fill, never replace**:
 *
 * - A prose field the person left blank takes the model's.
 * - A prose field the person wrote in is left exactly alone.
 * - Acceptance criteria are taken only when there are none.
 * - The description is **never rewritten**. The person's note stays first and verbatim,
 *   and the model's working specification is appended under an attribution line, above
 *   the provenance footer.
 *
 * Task-175's own statement of what would kill that feature -- "losing the one true
 * sentence someone dictated, inside a wall of generated prose" -- is the failure this
 * path could reintroduce, and a fill-only merge is what makes it unreachable rather than
 * merely unlikely.
 *
 * **Nothing here can set state.** The fields below are prose and acceptance criteria;
 * `modelaccess/draft.py` has no lifecycle, priority, parent, dependency or tag in its
 * output shape at all, and a capture files as a `draft` for a human to promote whether
 * or not a model touched it.
 */

/** Where the tray's drafting has got to for one finding. Plain data: it is persisted. */
export type TrayDraft =
  /** Asked for, not back yet. */
  | { state: "pending" }
  /** Merged in. `filled` names the fields that changed, for the card to say. */
  | { state: "applied"; model: string | null; filled: Array<DraftableField> }
  /** Not drafted, and why -- short enough to sit on a card. Never blocks filing. */
  | { state: "declined"; detail: string };

/** The prose fields a tray draft may fill. `description` is appended, never set. */
const FILLABLE = ["summary", "intent", "constraints", "out_of_scope"] as const;

/** The line that separates somebody's own words from a model's, inside one description. */
export function attribution(model: string | null): string {
  return (
    `**Fleshed out below by ${model ? `\`${model}\`` : "a model"}, from the note above.** ` +
    "Everything above this line is as it was typed."
  );
}

/** "Summary, Intent and Acceptance criteria" -- what the card says was fleshed out. */
export function nameFilled(fields: ReadonlyArray<DraftableField>): string {
  const labels = fields.map((field) => FIELD_LABELS[field]);
  if (labels.length <= 1) return labels[0] ?? "";
  return `${labels.slice(0, -1).join(", ")} and ${labels[labels.length - 1]}`;
}

/**
 * Merge a draft into the request a collected finding already carries.
 *
 * Returns a new request and the fields that changed; the caller stores both. An empty
 * `filled` means the draft had nothing to add that the person had not written -- which
 * is a real outcome, not a failure, and the card says so.
 */
export function mergeDraft(
  request: TaskCreateRequest,
  draft: SpecDraftResponse,
  model: string | null,
): { request: TaskCreateRequest; filled: Array<DraftableField> } {
  const merged: TaskCreateRequest = { ...request };
  const filled: Array<DraftableField> = [];

  for (const field of FILLABLE) {
    const incoming = (draft[field] ?? "").trim();
    // A field the person wrote in is theirs. A model that returned nothing for it has
    // declined to write one, which is not the same as deciding it should be empty.
    if (!incoming || (merged[field] ?? "").trim()) continue;
    merged[field] = incoming;
    filled.push(field);
  }

  const criteria = (draft.acceptance ?? [])
    .map((text) => text.trim())
    .filter(Boolean);
  if (criteria.length > 0 && !merged.acceptance?.length) {
    merged.acceptance = criteria.map((text, index) => ({
      id: `ac-${index + 1}`,
      text,
      status: "pending" as const,
    }));
    filled.push("acceptance");
  }

  const body = (draft.description ?? "").trim();
  if (body) {
    // Appended rather than assigned: the description is the one field the person always
    // filled in, because the form requires it.
    merged.description = insertAboveProvenance(
      merged.description ?? "",
      `${attribution(model)}\n\n${body}`,
    );
    filled.push("description");
  }

  return { request: merged, filled };
}
