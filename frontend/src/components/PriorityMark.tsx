import type { CSSProperties } from "react";

/**
 * How a priority looks, everywhere it is shown (task-563).
 *
 * **A priority must never be mistaken for a status.** task-562 made the status chip a
 * filled, bordered box whose colour carries meaning -- brown will start, red needs you.
 * Before this, priority was drawn the same way: a filled box beside the status chip, so
 * a red "critical" read like a second red "Blocked". Priority now has its own *form*,
 * which is what tells the two apart without relying on colour:
 *
 * * **No box.** No fill and no border; the status chip is the only boxed thing on a row.
 * * **Rising bars** -- one to four filled, like a signal meter. The number of bars is
 *   the priority, so the mark reads correctly in greyscale and to a colour-blind reader.
 * * **Uppercase, letter-spaced text.** Status words are sentence case (`chipCase`), so
 *   the case alone separates the two vocabularies.
 *
 * The hues are allowed to echo the status palette; the form is what must not.
 *
 * The sidebar's band headers ("CRITICAL TASKS") use this same mark, so a header and the
 * priority on a task page read as one system. One colour map, here and nowhere else.
 */

export const PRIORITIES = ["critical", "high", "medium", "low"] as const;
export type PriorityName = (typeof PRIORITIES)[number];

/** Literal colours, like the status palette: a functional layer, not a theme. */
export const PRIORITY_COLOURS: Record<PriorityName, string> = {
  critical: "#f87171",
  high: "#fb923c",
  medium: "#fbbf24",
  low: "#94a3b8",
};

/** How many of the four bars are filled. */
const LEVEL: Record<PriorityName, number> = { critical: 4, high: 3, medium: 2, low: 1 };

/**
 * A priority this bundle knows, from whatever the server sent.
 *
 * Absent means medium, which is what the server defaults to. A word newer than this
 * bundle also draws as medium rather than failing: the mark is one element of a page.
 */
export function priorityName(value: string | null | undefined): PriorityName {
  return (PRIORITIES as ReadonlyArray<string>).includes(value ?? "")
    ? (value as PriorityName)
    : "medium";
}

function Bars({ level }: { level: number }) {
  return (
    <svg
      aria-hidden="true"
      data-priority-bars={level}
      width="12"
      height="10"
      viewBox="0 0 12 10"
      className="shrink-0"
    >
      {[0, 1, 2, 3].map((index) => (
        <rect
          key={index}
          x={index * 3}
          y={7 - index * 2}
          width="2"
          height={3 + index * 2}
          fill="currentColor"
          opacity={index < level ? 1 : 0.25}
        />
      ))}
    </svg>
  );
}

/**
 * The priority mark: bars and an uppercase word, in the band's colour, with no box.
 *
 * `suffix` extends the word -- the band headers pass " TASKS". The text is literal
 * uppercase rather than a CSS transform, so what a test reads is what a browser shows.
 */
export function PriorityMark({
  priority,
  suffix = "",
  className = "",
  style,
}: {
  priority: string | null | undefined;
  suffix?: string;
  className?: string;
  style?: CSSProperties;
}) {
  const name = priorityName(priority);
  return (
    <span
      data-priority={name}
      title={suffix ? undefined : `Priority: ${name}`}
      className={`inline-flex items-center gap-1 whitespace-nowrap text-xs font-semibold tracking-wider ${className}`}
      style={{ color: PRIORITY_COLOURS[name], ...style }}
    >
      <Bars level={LEVEL[name]} />
      <span>{`${name.toUpperCase()}${suffix}`}</span>
    </span>
  );
}
