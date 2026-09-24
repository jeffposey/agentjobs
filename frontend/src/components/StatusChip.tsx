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
export function chipCase(label: string | null | undefined): string {
  // Absent from a server older than the field that carries it. A chip is one element of
  // a page; an empty one is a defect to notice, a thrown one takes the whole page down.
  const word = label ?? "";
  return word.charAt(0).toUpperCase() + word.slice(1);
}

/**
 * The one motion a status chip can have (task-570), and the class it draws with. The
 * keyframes are in `styles.css`; this is the only place a state is mapped to it.
 *
 * **Motion means one thing: something is happening to this task right now, and a live
 * fact says so.** Never the lifecycle alone -- "Working" is derived from the record, and
 * a task whose session died still reads it -- so every function below asks for the fact
 * that backs the word: the run's own health, the queue's `starting`, a live finish.
 *
 * **One motion for every live state** (owner decision, 2026-09-24): a comet running round
 * the border. The first cut gave Working, Starting and Finishing a motion each, and three
 * motions on one page read as three things to learn rather than one signal. The chip's
 * colour already says which kind of activity it is.
 *
 * It is a pseudo-element, so a chip never changes size, and `prefers-reduced-motion`
 * stills it to a static outer ring.
 */
export type ChipMotion = "orbit";

export const MOTION_CLASSES: Record<ChipMotion, string> = {
  orbit: "chip-motion chip-motion-orbit",
};

/** Run-board health words that are activity. `handback` is a wait, not work, so it is absent. */
const HEALTH_MOTION: Record<string, ChipMotion> = {
  working: "orbit",
  starting: "orbit",
  finishing: "orbit",
};

/** A run's chip: its health word is itself the live fact. */
export function runMotion(health: string): ChipMotion | null {
  return HEALTH_MOTION[health] ?? null;
}

/** The facts a task read carries that can back a moving chip. Every task read model has them. */
type MotionFacts = {
  status_category: StatusCategory;
  live_finish?: unknown;
  queued_dispatch?: { status?: string | null } | null;
  live_run_health?: string | null;
};

/**
 * A task's chip. The category must agree with the fact as well as the fact being there:
 * a task handed to review while its session is still open has a working run and a red
 * chip, and a red chip does not move.
 */
export function taskMotion(task: MotionFacts): ChipMotion | null {
  const live =
    (task.status_category === "finishing" && Boolean(task.live_finish)) ||
    (task.status_category === "queued" && task.queued_dispatch?.status === "starting") ||
    (task.status_category === "working" &&
      (task.live_run_health === "working" || task.live_run_health === "starting"));
  return live ? "orbit" : null;
}

/** The chip's classes, with its motion when it has one. */
export function chipClasses(motion?: ChipMotion | null): string {
  return motion ? `${CHIP_SHAPE} ${MOTION_CLASSES[motion]}` : CHIP_SHAPE;
}

export function StatusChip({
  category,
  label,
  title,
  testId,
  motion,
}: {
  category: StatusCategory;
  label: string;
  title?: string;
  testId?: string;
  motion?: ChipMotion | null;
}) {
  return (
    <span
      data-status-category={category}
      data-motion={motion ?? undefined}
      data-testid={testId}
      title={title}
      className={chipClasses(motion)}
      style={categoryStyle(category)}
    >
      {chipCase(label)}
    </span>
  );
}
