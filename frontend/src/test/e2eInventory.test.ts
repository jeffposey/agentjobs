import { describe, expect, it } from "vitest";

// The audit, as a string. `?raw` rather than `node:fs` because this project has no Node
// types -- the same trick `push.test.ts` and `PrimaryNav.test.tsx` already use.
import audit from "../../e2e/README.md?raw";

/**
 * The end-to-end audit and the end-to-end directory have to agree (task-369).
 *
 * `e2e/README.md` classifies every spec by what it touches -- read-only, the project,
 * machine-level state, or the built bundle -- and that table is the reasoning behind
 * running the suite on four workers with a server each. A table that has quietly
 * stopped describing the directory is worse than no table: it reads as though somebody
 * checked.
 *
 * So this test fails on a spec that was added without a row, and on a row left behind by
 * a spec that was deleted or renamed. It deliberately proves nothing about whether a
 * classification is *correct* -- no test can -- only that a human was made to write one
 * down.
 */

/** Every spec file on disk, by name. `import.meta.glob` lists without importing. */
const onDisk = Object.keys(import.meta.glob("../../e2e/*.spec.ts"))
  .map((path) => path.slice(path.lastIndexOf("/") + 1))
  .sort();

const KINDS = ["read-only", "project", "machine", "bundle"];

/** Every row of the audit's table, as `{ spec, kind }`. */
const rows = audit
  .split("\n")
  .map((line) => /^\|\s*`([^`]+\.spec\.ts)`\s*\|\s*([^|]+?)\s*\|/.exec(line))
  .filter((match): match is RegExpExecArray => match !== null)
  .map((match) => ({ spec: match[1] as string, kind: match[2] as string }));

describe("the end-to-end audit", () => {
  it("has a row for every spec, and a spec for every row", () => {
    expect(onDisk.length).toBeGreaterThan(0);
    expect(rows.map((row) => row.spec).sort()).toEqual(onDisk);
  });

  it("classifies each spec as one of the three kinds", () => {
    for (const row of rows) {
      expect(KINDS, `${row.spec} is classified "${row.kind}"`).toContain(row.kind);
    }
  });

  it("says what makes each spec what it is", () => {
    for (const line of audit.split("\n")) {
      const match = /^\|\s*`([^`]+\.spec\.ts)`\s*\|[^|]+\|\s*([^|]*?)\s*\|/.exec(line);
      if (match === null) continue;
      expect(match[2]!.length, `${match[1]} has an empty reason`).toBeGreaterThan(20);
    }
  });
});
