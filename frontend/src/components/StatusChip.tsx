import type { StatusCategory } from "../api/types";

/**
 * The status colours: one per category, and every status in exactly one category
 * (task-562).
 *
 * The server decides both the word (`display_status`) and the category
 * (`status_category`) in one function, so a chip can only disagree with another chip
 * if a component picks its own colour or rewrites the word -- which is the whole of
 * what this file exists to stop. Every status chip on every surface draws from here.
 *
 * | Category          | Colour | Labels                                           |
 * |-------------------|--------|--------------------------------------------------|
 * | ready             | green  | Ready                                            |
 * | queued            | brown  | Queued, Starting                                 |
 * | working           | blue   | Working                                          |
 * | finishing         | purple | Finishing                                        |
 * | needs_you         | red    | Needs spec/review/decision/approval/input, Dependency data error |
 * | not_now           | pink   | Blocked, On hold, Sub-tasks, Quota reset, Draft  |
 * | closed            | grey   | Completed                                        |
 * | closed_unfinished | grey, struck through | Superseded, Cancelled, Duplicate   |
 *
 * **Grey means closed and nothing else**, so any coloured chip is a live task.
 *
 * **A functional layer, not a theme** (owner decision, 2026-09-24): these colours say
 * what a task's situation is and apply in every theme, so they are literal palette
 * classes and deliberately not the app's `dark-*` tokens. A future theme system owns
 * surfaces, text and borders; it must not reach in here.
 *
 * The brown is the amber-900 fill the slot board's queued rail has always used.
 */
export const CATEGORY_CLASSES: Record<StatusCategory, string> = {
  ready: "border-emerald-600 bg-emerald-900 text-emerald-100",
  queued: "border-amber-700 bg-amber-900 text-amber-100",
  working: "border-blue-600 bg-blue-900 text-blue-100",
  finishing: "border-violet-600 bg-violet-900 text-violet-100",
  needs_you: "border-red-600 bg-red-900 text-red-100",
  not_now: "border-pink-600 bg-pink-900 text-pink-100",
  closed: "border-slate-600 bg-slate-800 text-slate-300",
  // The same grey: closed is one category. The strikethrough is on the text, so the
  // word still carries the meaning for a reader who cannot tell the colours apart.
  closed_unfinished: "border-slate-600 bg-slate-800 text-slate-300 line-through",
};

/** The shape every status chip has, so no surface draws a filled one beside an outlined one. */
export const CHIP_SHAPE = "inline-flex whitespace-nowrap rounded border px-2 py-0.5 text-xs font-medium";

/** The classes for a chip of this category: shape and colour together. */
export function statusChipClasses(category: StatusCategory): string {
  return `${CHIP_SHAPE} ${CATEGORY_CLASSES[category]}`;
}

/**
 * Capitalise the first letter and leave the rest alone.
 *
 * The chip owns letter case (owner decision, 2026-09-24): every status label is
 * capitalised, so a lowercase source -- a walk's state, an outcome -- cannot reach the
 * screen lowercase. The rest is left as written because a label like "Needs spec" is
 * already the server's exact word.
 */
export function chipCase(label: string): string {
  return label.charAt(0).toUpperCase() + label.slice(1);
}

export function StatusChip({
  category,
  label,
  title,
  testId,
}: {
  category: StatusCategory;
  label: string;
  title?: string;
  testId?: string;
}) {
  return (
    <span
      data-status-category={category}
      data-testid={testId}
      title={title}
      className={statusChipClasses(category)}
    >
      {chipCase(label)}
    </span>
  );
}
