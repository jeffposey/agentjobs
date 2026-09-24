import { describe, expect, it } from "vitest";

import inventory from "../../status-surfaces.md?raw";
import { HEALTH_LABELS } from "../components/LiveRuns";
import { MOTION_CLASSES, runMotion, taskMotion } from "../components/StatusChip";

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
  /\bdisplay_status\b|\bstatus_category\b|\blive_finish\b|\bHealthBadge\b|\bFinishBadge\b|\bdependencyState\b|<DependencyState\b|\.health\b|\bStatusChip\b|\bCATEGORY_CLASSES\b|\bstatusChipClasses\b/;

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
      if (path === "components/StatusChip.tsx") continue;
      expect(code(text), `${path} spells the finishing fill itself`).not.toMatch(/\bbg-violet-900\b(?!\/)/);
    }
  });

  it("defines the category-to-colour map in one place", () => {
    // task-562: four components each chose their own colours for the same status.
    for (const [path, text] of Object.entries(sources)) {
      if (path === "components/StatusChip.tsx") continue;
      expect(code(text), `${path} defines its own status colour map`).not.toMatch(
        /Record<StatusCategory,\s*string>/,
      );
    }
  });

  it("no longer rewrites the server's word anywhere", () => {
    // task-562 ac-3: the words the list used to overwrite `display_status` with.
    for (const [path, text] of Object.entries(sources)) {
      for (const retired of ["Actionable now", "In flight", "Waiting on sub-tasks"]) {
        expect(code(text), `${path} spells "${retired}"`).not.toContain(retired);
      }
    }
  });

  it("draws every display_status chip through the one chip", () => {
    for (const [path, text] of Object.entries(sources)) {
      if (!/\bdisplay_status\b/.test(code(text))) continue;
      expect(code(text), `${path} renders display_status without the status chip`).toMatch(
        /\bStatusChip\b|<DependencyState\b|\bdependencyState\b/,
      );
    }
  });

  it("gives a finishing run the word the server gives a finishing task", () => {
    // `derived_display_status` returns "Finishing"; tests/test_status_surfaces.py reads
    // this same map from the Python side, so the two cannot drift apart silently.
    expect(HEALTH_LABELS.finishing).toBe("Finishing");
  });
});

describe("the chips that move (task-570)", () => {
  it("maps a state to a motion in one place", () => {
    // A second file naming a motion or its class is a surface deciding for itself what
    // counts as "happening now" -- the drift task-533 found with the word "Finishing".
    for (const [path, text] of Object.entries(sources)) {
      if (path === "components/StatusChip.tsx") continue;
      expect(code(text), `${path} names a chip motion itself`).not.toMatch(
        /chip-motion|["'](?:orbit|ignite|sweep)["']/,
      );
    }
  });

  it("keeps keyframes in the stylesheet, not in a component", () => {
    for (const [path, text] of Object.entries(sources)) {
      expect(code(text), `${path} defines an animation inline`).not.toMatch(
        /@keyframes|animate-[a-z]|animation\s*:|requestAnimationFrame/,
      );
    }
  });

  it("gives each kind of activity its own motion", () => {
    const kinds = [runMotion("working"), runMotion("starting"), runMotion("finishing")];
    expect(new Set(kinds).size).toBe(3);
    expect(kinds.every((kind) => kind !== null && MOTION_CLASSES[kind] !== undefined)).toBe(true);
  });

  it("moves no run chip that is a wait or a process state", () => {
    for (const health of ["handback", "parked", "silent", "idle", "orphaned", "work_done", "unknown"]) {
      expect(runMotion(health), health).toBeNull();
    }
  });

  it("moves a task chip only when a live fact backs it", () => {
    const working = { status_category: "working" as const };
    expect(taskMotion({ ...working, live_run_health: "working" })).toBe(runMotion("working"));
    expect(taskMotion({ ...working, live_run_health: "starting" })).toBe(runMotion("working"));
    // "Working" from the record alone: the session is gone, parked or silent.
    expect(taskMotion(working)).toBeNull();
    for (const health of ["parked", "silent", "orphaned", "handback", "idle"]) {
      expect(taskMotion({ ...working, live_run_health: health }), health).toBeNull();
    }

    const finish = { current_step: "gate" };
    expect(taskMotion({ status_category: "finishing", live_finish: finish })).toBe(runMotion("finishing"));
    expect(taskMotion({ status_category: "finishing" })).toBeNull();

    expect(taskMotion({ status_category: "queued", queued_dispatch: { status: "starting" } })).toBe(
      runMotion("starting"),
    );
    expect(taskMotion({ status_category: "queued", queued_dispatch: { status: "waiting" } })).toBeNull();

    // A live run under a chip that is not "Working" -- review, hold, closed -- stays still.
    for (const category of ["needs_you", "not_now", "ready", "draft", "closed", "closed_unfinished"] as const) {
      expect(taskMotion({ status_category: category, live_run_health: "working" }), category).toBeNull();
    }
  });
});
