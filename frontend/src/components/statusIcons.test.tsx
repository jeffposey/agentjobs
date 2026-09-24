import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import vocabulary from "../../../src/agentjobs/status_vocabulary.json";
import { FinishBadge, HealthBadge } from "./LiveRuns";
import { StatusChip } from "./StatusChip";
import { STATUS_ICONS } from "./statusIcons";

/**
 * Status icons (task-578): the data file names them and the registry can draw them.
 *
 * **No chip draws one.** The owner tried an icon inside the chip and then beside it, and
 * rejected both: next to the word, the glyph was clutter rather than information. The
 * choices are kept in the data file for a use that is not next to the word, which is its
 * own decision. Until then this holds the data valid and the chips icon-free.
 */

type Entry = { label: string; category: string; icon?: string };

const SECTIONS = {
  statuses: vocabulary.statuses,
  run_health: vocabulary.run_health,
  walk: vocabulary.walk,
} as Record<string, Record<string, Entry>>;

const ENTRIES = Object.entries(SECTIONS).flatMap(([section, entries]) =>
  Object.entries(entries).map(([key, entry]) => ({ where: `${section}.${key}`, ...entry })),
);

/** The chip's shape, byte for byte. */
const CHIP = "inline-flex whitespace-nowrap rounded border px-2 py-0.5 text-xs font-medium";

describe("the icon registry", () => {
  it("has every icon the data file names", () => {
    for (const entry of ENTRIES.filter((e) => e.icon)) {
      expect(STATUS_ICONS, `${entry.where} names "${entry.icon}", which is not registered`).toHaveProperty(
        [entry.icon as string],
      );
    }
  });

  it("carries only the icons the data file names", () => {
    const used = new Set(ENTRIES.map((e) => e.icon).filter(Boolean));
    expect(Object.keys(STATUS_ICONS).filter((name) => !used.has(name))).toEqual([]);
  });

  it("gives a word one icon wherever the word appears", () => {
    // Working is a task status and a run's health; whatever draws the icon later must
    // not get a different answer depending on which section it reads.
    const byLabel = new Map<string, Set<string | undefined>>();
    for (const entry of ENTRIES) {
      byLabel.set(entry.label, (byLabel.get(entry.label) ?? new Set()).add(entry.icon));
    }
    for (const [label, icons] of byLabel) {
      expect([...icons], `"${label}" has more than one icon`).toHaveLength(1);
    }
  });
});

describe("the chips", () => {
  it("draw no icon beside or inside the word", () => {
    const rendered = [
      render(<StatusChip category="finishing" label="Landing" />).container,
      render(<HealthBadge health="parked" />).container,
      render(
        <FinishBadge
          finish={{ task_id: "task-1", detail: "gate", overtaken: false } as Parameters<typeof FinishBadge>[0]["finish"]}
        />,
      ).container,
    ];
    for (const container of rendered) {
      const chip = container.firstElementChild as HTMLElement;
      expect(container.querySelector("svg")).toBeNull();
      expect(chip).toHaveAttribute("data-status-category");
      expect(chip.className.startsWith(CHIP)).toBe(true);
      expect(chip.childNodes).toHaveLength(1);
    }
  });
});
