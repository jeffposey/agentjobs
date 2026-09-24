import type { CSSProperties } from "react";

import type { StatusCategory } from "../api/types";
// The one data file every status word and colour comes from (task-562). The server reads
// it for the words it sends as `display_status`; this reads it for the colours.
import vocabulary from "../../../src/agentjobs/status_vocabulary.json";

/**
 * The status colours: one per category, and every status in exactly one category.
 *
 * The server decides both the word (`display_status`) and the category
 * (`status_category`) in one function, from `status_vocabulary.json`, so a chip can only
 * disagree with another chip if a component picks its own colour or rewrites the word --
 * which is the whole of what this file exists to stop. Every status chip on every
 * surface draws from here.
 *
 * **Grey means closed and nothing else**, so any coloured chip is a live task. Completed
 * is solid grey; an ending without the work done is hollow, with a dashed border.
 *
 * **A functional layer, not a theme** (owner decision, 2026-09-24): these colours say
 * what a task's situation is and apply in every theme, so they are literal colours from
 * the data file and deliberately not the app's `dark-*` tokens. They are inline styles
 * rather than Tailwind classes so the file is the only place a colour is written.
 */
type Colours = { fill: string; border: string; text: string; border_style?: string };

const CATEGORIES = vocabulary.categories as Record<StatusCategory, Colours>;

export function categoryStyle(category: StatusCategory): CSSProperties {
  // Absent from a server older than this field, or a category newer than this bundle:
  // the word still says what it is, so the chip draws uncoloured rather than failing.
  const colours = CATEGORIES[category] as Colours | undefined;
  if (!colours) return {};
  return {
    backgroundColor: colours.fill,
    borderColor: colours.border,
    borderStyle: colours.border_style ?? "solid",
    color: colours.text,
  };
}

type Entry = { label: string; category: StatusCategory };

/** A run's health word and category, where the health names a task status. */
export const RUN_HEALTH = vocabulary.run_health as Record<string, Entry>;

/** An epic walk's badge word and category. */
export const WALK_STATES = vocabulary.walk as Record<"walking" | "waiting" | "grounded", Entry>;

/** The shape every status chip has, so no surface draws a filled one beside an outlined one. */
export const CHIP_SHAPE = "inline-flex whitespace-nowrap rounded border px-2 py-0.5 text-xs font-medium";

/**
 * Capitalise the first letter and leave the rest alone.
 *
 * The chip owns letter case (owner decision, 2026-09-24): every status label is
 * capitalised, so a lowercase source cannot reach the screen lowercase. The rest is left
 * as written because a label like "Needs spec" is already the data file's exact word.
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
      className={CHIP_SHAPE}
      style={categoryStyle(category)}
    >
      {chipCase(label)}
    </span>
  );
}
