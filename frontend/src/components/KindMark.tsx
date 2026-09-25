import { Code, DraftingCompass } from "lucide-react";

/**
 * How a task's kind looks, everywhere it is shown (task-593, designed in task-556).
 *
 * A row already carries two marks, and a third must not be mistaken for either. Each has
 * its own *form*, so none of them depends on colour (the rule `PriorityMark` states):
 *
 * * **status** -- a filled, bordered box, sentence case.
 * * **priority** -- rising bars with no box, UPPERCASE.
 * * **kind** -- a **dashed-outline pill** with an icon and the word, sentence case. The
 *   dashed border is the one thing no other mark draws; the icon and the word are both
 *   always present, so the mark reads in greyscale and to a screen reader alike.
 *
 * Where it appears is the caller's choice, and deliberately differs: the list marks only
 * `design` (implementation is most rows, and a mark on all of them is noise), while the
 * record header names both so an unmarked record is never ambiguous.
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
const ICON = { design: DraftingCompass, implementation: Code } as const;

export function kindLabel(value: string | null | undefined): string {
  return LABEL[kindName(value)];
}

/**
 * The kind mark: a dashed pill holding a 14px icon and the word.
 *
 * `whitespace-nowrap` on the pill is what keeps the icon and the word together when the
 * chip row it sits in wraps at phone width.
 */
export function KindMark({
  kind,
  className = "",
}: {
  kind: string | null | undefined;
  className?: string;
}) {
  const name = kindName(kind);
  const Icon = ICON[name];
  return (
    <span
      data-kind={name}
      title={`Kind: ${LABEL[name].toLowerCase()}`}
      className={`inline-flex shrink-0 items-center gap-1 whitespace-nowrap rounded-full border border-dashed border-violet-400/70 px-1.5 text-xs text-violet-200 ${className}`}
    >
      <Icon aria-hidden="true" focusable="false" size={14} strokeWidth={2} className="shrink-0" />
      <span>{LABEL[name]}</span>
    </span>
  );
}
