import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import vocabulary from "../../../src/agentjobs/status_vocabulary.json";
import { HealthBadge } from "./LiveRuns";
import { LABEL_ICONS, StatusChip } from "./StatusChip";
import { STATUS_ICONS } from "./statusIcons";

/**
 * Status icons (task-578): the data file names them, the registry draws them, and a chip
 * with none is the chip it was before icons existed.
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

/** The chip's markup before task-578, byte for byte: no icon, no gap, one text node. */
const PRE_ICON_SHAPE = "inline-flex whitespace-nowrap rounded border px-2 py-0.5 text-xs font-medium";

describe("the icon registry", () => {
  it("has every icon the data file names", () => {
    // A name missing here would draw nothing on the page; this is where it is an error.
    for (const entry of ENTRIES.filter((e) => e.icon)) {
      expect(STATUS_ICONS, `${entry.where} names "${entry.icon}", which is not registered`).toHaveProperty(
        [entry.icon as string],
      );
    }
  });

  it("carries only the icons the data file names, so the bundle carries only those", () => {
    const used = new Set(ENTRIES.map((e) => e.icon).filter(Boolean));
    expect(Object.keys(STATUS_ICONS).filter((name) => !used.has(name))).toEqual([]);
  });

  it("gives a word one icon wherever the word appears", () => {
    // The chip looks its icon up by word, so Working as a task and Working as a run
    // must agree or the lookup would depend on which section was read last.
    const byLabel = new Map<string, Set<string | undefined>>();
    for (const entry of ENTRIES) {
      byLabel.set(entry.label, (byLabel.get(entry.label) ?? new Set()).add(entry.icon));
    }
    for (const [label, icons] of byLabel) {
      expect([...icons], `"${label}" has more than one icon`).toHaveLength(1);
    }
  });

  it("maps the Landing word to the plane-landing icon", () => {
    expect(LABEL_ICONS[vocabulary.statuses.finishing.label]).toBe("plane-landing");
  });
});

describe("a chip with an icon", () => {
  it("draws it beside the chip, in the category's border colour, hidden from assistive technology", () => {
    const { container } = render(<StatusChip category="finishing" label="Landing" />);
    const pair = container.firstElementChild as HTMLElement;
    const [icon, chip] = Array.from(pair.children) as [SVGElement, HTMLElement];

    expect(icon.tagName.toLowerCase()).toBe("svg");
    expect(icon).toHaveAttribute("data-status-icon", "plane-landing");
    expect(icon).toHaveAttribute("aria-hidden", "true");
    expect(icon.style.color).not.toBe("");
    expect(chip).toHaveAttribute("data-status-category", "finishing");
    expect(chip).toHaveTextContent(/^Landing$/);
  });

  it("leaves the chip itself exactly as it was, so the icon cannot crowd the word", () => {
    // The owner's revision: inside the chip, the icon squeezed the task sidebar's
    // already-smaller chip until its word was hard to read.
    const { container } = render(<StatusChip category="finishing" label="Landing" />);
    const chip = container.querySelector("[data-status-category]") as HTMLElement;
    expect(chip.className).toBe(PRE_ICON_SHAPE);
    expect(chip.childNodes).toHaveLength(1);
    expect(chip.querySelector("svg")).toBeNull();
  });

  it("draws a run's health icon on the run board too, outside its badge", () => {
    const { container } = render(<HealthBadge health="parked" />);
    expect(container.querySelector("[data-status-icon]")).toHaveAttribute("data-status-icon", "hand");
    expect(container.querySelector("[data-health] svg")).toBeNull();
  });
});

describe("a chip with no icon", () => {
  it("renders exactly as it did before icons: no placeholder, no gap", () => {
    // Grounded is deliberately left without one in the data file, so this is a real state.
    expect((vocabulary.walk.grounded as Entry).icon).toBeUndefined();
    const { container } = render(<StatusChip category="needs_you" label="Grounded" />);
    const chip = container.firstElementChild as HTMLElement;

    expect(chip.className).toBe(PRE_ICON_SHAPE);
    expect(chip.childNodes).toHaveLength(1);
    expect(chip.firstChild?.nodeType).toBe(Node.TEXT_NODE);
    expect(chip.firstChild?.textContent).toBe("Grounded");
  });

  it("draws nothing for a name the registry lacks, rather than failing the page", () => {
    const { container } = render(<StatusChip category="ready" label="Ready" icon="no-such-icon" />);
    const chip = container.firstElementChild as HTMLElement;
    expect(chip.querySelector("svg")).toBeNull();
    expect(chip.className).toBe(PRE_ICON_SHAPE);
  });
});
