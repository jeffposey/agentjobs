import { describe, expect, it } from "vitest";

import inventory from "../../status-surfaces.md?raw";
import { HEALTH_LABELS } from "../components/LiveRuns";

/**
 * Every surface that renders a task's state is named, and draws it from the agreed
 * source (task-533).
 *
 * Task-509 fixed "Finishing" on the five surfaces it listed and missed the slot board's
 * run tile, so the same report arrived twice. This is the check that would have caught
 * the sixth: a file that starts rendering state without a row in
 * `frontend/status-surfaces.md` goes red, and so does a file that spells a status word, a
 * run's raw `health`, or the finishing colour for itself.
 *
 * It proves nothing about whether a row is *right* -- no test can -- only that someone
 * wrote down where the word comes from before shipping a new place that shows it.
 */

/** Every non-test source file, as text. `?raw` because this project has no Node types. */
const sources = Object.fromEntries(
  Object.entries(
    import.meta.glob<string>(["../**/*.tsx", "!../**/*.test.tsx", "!../api/generated/**"], {
      query: "?raw",
      import: "default",
      eager: true,
    }),
  ).map(([path, text]) => [path.replace(/^\.\.\//, ""), text]),
);

/** What marks a file as rendering a task's or a run's state. */
const RENDERS_STATE =
  /\bdisplay_status\b|\blive_finish\b|\bHealthBadge\b|\bFinishBadge\b|\bdependencyState\b|<DependencyState\b|\.health\b|\bFINISHING_FILL\b/;

const rows = inventory
  .split("\n")
  .map((line) => /^\|\s*`([^`]+\.tsx)`\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|/.exec(line))
  .filter((match): match is RegExpExecArray => match !== null)
  .map((match) => ({ file: match[1]!, source: match[3]! }));

/** Comments removed, so prose explaining a rule is not read as breaking it. */
function code(text: string): string {
  return text.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:"'`])\/\/.*$/gm, "$1");
}

describe("the status surface inventory", () => {
  it("names every file that renders a task's or a run's state, and no other", () => {
    const rendering = Object.entries(sources)
      .filter(([, text]) => RENDERS_STATE.test(code(text)))
      .map(([path]) => path)
      .sort();

    expect(rendering.length).toBeGreaterThan(5);
    expect(rows.map((row) => row.file).sort()).toEqual(rendering);
  });

  it("says where each surface's word comes from", () => {
    const allowed = ["display_status", "dependencyState", "health", "live_finish", "the finish read"];
    for (const row of rows) {
      expect(allowed, `${row.file} draws its state from "${row.source}"`).toContain(
        row.source.replace(/`/g, ""),
      );
    }
  });
});

describe("the agreed sources", () => {
  it("spells Finishing only where the word is defined", () => {
    // The server's `display_status` is the task's word, and HEALTH_LABELS / FinishBadge
    // are the run's. A third spelling is a surface deciding the word for itself.
    const allowed = new Set(["components/LiveRuns.tsx", "components/TaskList.tsx"]);
    for (const [path, text] of Object.entries(sources)) {
      if (allowed.has(path)) continue;
      expect(code(text), `${path} spells "Finishing" itself`).not.toMatch(/["'>]Finishing["'<]/);
    }
  });

  it("renders a run's health only through its label", () => {
    for (const [path, text] of Object.entries(sources)) {
      expect(code(text), `${path} renders a raw health value`).not.toMatch(/(?<!=)\{\s*[\w.?]+\.health\s*\}/);
    }
  });

  it("defines the finishing colour once", () => {
    for (const [path, text] of Object.entries(sources)) {
      if (path === "components/DependencyState.tsx") continue;
      expect(code(text), `${path} spells the finishing fill itself`).not.toMatch(/\bbg-violet-900\b(?!\/)/);
    }
  });

  it("gives a finishing run the word the server gives a finishing task", () => {
    // `derived_display_status` returns "Finishing"; tests/test_status_surfaces.py reads
    // this same map from the Python side, so the two cannot drift apart silently.
    expect(HEALTH_LABELS.finishing).toBe("Finishing");
  });
});
