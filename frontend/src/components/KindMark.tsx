/**
 * How a task's kind looks, everywhere it is shown (task-593, designed in task-556).
 *
 * A row already carries two marks, and a third must not be mistaken for either. Each has
 * its own *form*, so none of them depends on colour (the rule `PriorityMark` states):
 *
 * * **status** -- a solid-filled, squared box, sentence case.
 * * **priority** -- rising bars with no box, UPPERCASE.
 * * **kind** -- a **fully rounded, tinted pill with no outline**, holding the word in
 *   semibold sentence case. The word is always present.
 *
 * **Yellow, a word, and legible** (owner's reviews of task-593). Violet is the landing's
 * colour (`LandingProgress`). Yellow is the colour of work that is not yet code -- the
 * Draft chip is filled with it -- so a design mark sits in that family, told apart from
 * Draft by a translucent tint and no border, where Draft is a solid fill inside a box.
 * No icon: beside the word it said nothing the word did not. No dashed border and no
 * shrinking in a list row: both made the mark too hard to read. The owner chose this
 * form from four auditions in the review sandbox (option C of the second round).
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

/** The Draft chip's yellow (status_vocabulary.json), as a translucent tint. */
const COLOURS: Record<KindName, { text: string; fill: string }> = {
  design: { text: "#fde047", fill: "rgba(250, 204, 21, 0.22)" },
  implementation: { text: "#cbd5e1", fill: "rgba(100, 116, 139, 0.22)" },
};

export function kindLabel(value: string | null | undefined): string {
  return LABEL[kindName(value)];
}

/** The kind mark: a rounded, tinted pill holding the word. */
export function KindMark({
  kind,
  className = "",
}: {
  kind: string | null | undefined;
  className?: string;
}) {
  const name = kindName(kind);
  const colours = COLOURS[name];
  return (
    <span
      data-kind={name}
      title={`Kind: ${LABEL[name].toLowerCase()}`}
      className={`inline-flex shrink-0 items-center whitespace-nowrap rounded-full px-2 text-xs font-semibold leading-5 ${className}`}
      style={{ color: colours.text, backgroundColor: colours.fill }}
    >
      {LABEL[name]}
    </span>
  );
}
