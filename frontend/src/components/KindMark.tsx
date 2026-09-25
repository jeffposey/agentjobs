/**
 * How a task's kind looks, everywhere it is shown (task-593, designed in task-556).
 *
 * A row already carries two marks, and a third must not be mistaken for either. Each has
 * its own *form*, so none of them depends on colour (the rule `PriorityMark` states):
 *
 * * **status** -- a filled, bordered box, sentence case.
 * * **priority** -- rising bars with no box, UPPERCASE.
 * * **kind** -- a **dashed-outline pill** holding the word, sentence case. The dashed
 *   border is the one thing no other mark draws, and the word is always present.
 *
 * **Yellow, and no icon** (owner's review of task-593). Violet is the landing's colour
 * (`LandingProgress`), so a violet mark read as part of a finish. Yellow is the colour of
 * work that is not yet code -- the Draft chip is filled with it -- so a design mark sits
 * in that family, told apart from Draft by being an outline rather than a fill. An icon
 * beside the word added nothing the word did not already say.
 *
 * Where it appears is the caller's choice, and deliberately differs: the list marks only
 * `design` (implementation is most rows, and a mark on all of them is noise), while the
 * record header names both so an unmarked record is never ambiguous. Implementation is
 * drawn in neutral grey: it is the default, and naming it should not draw the eye.
 */

export const KINDS = ["design", "implementation"] as const;
export type KindName = (typeof KINDS)[number];

/**
 * A kind this bundle knows, from whatever the server sent.
 *
 * Absent means implementation -- that is the field's contract (task-592) -- and so does a
 * word newer than this bundle: the mark is one element of a page and must not fail it.
 */
export function kindName(value: string | null | undefined): KindName {
  return value === "design" ? "design" : "implementation";
}

const LABEL: Record<KindName, string> = { design: "Design", implementation: "Implementation" };

/** The Draft chip's yellow (status_vocabulary.json), as an outline rather than a fill. */
const COLOURS: Record<KindName, { border: string; text: string }> = {
  design: { border: "#fde047", text: "#facc15" },
  implementation: { border: "#64748b", text: "#94a3b8" },
};

export function kindLabel(value: string | null | undefined): string {
  return LABEL[kindName(value)];
}

/** The kind mark: a dashed pill holding the word. */
export function KindMark({
  kind,
  className = "",
}: {
  kind: string | null | undefined;
  className?: string;
}) {
  const name = kindName(kind);
  return (
    <span
      data-kind={name}
      title={`Kind: ${LABEL[name].toLowerCase()}`}
      className={`inline-flex shrink-0 items-center whitespace-nowrap rounded-full border border-dashed px-1.5 text-xs font-medium ${className}`}
      style={{ borderColor: COLOURS[name].border, color: COLOURS[name].text }}
    >
      {LABEL[name]}
    </span>
  );
}
