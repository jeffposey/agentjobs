/**
 * Check the generated API client, and say which of two different things is wrong.
 *
 * The check this replaces was `git diff --quiet -- src/api/generated`, run after
 * `openapi-ts` had already overwritten those files. That compares the working tree
 * against **HEAD**, so it failed identically for two unrelated situations -- a client
 * that does not match the schema, and a client that matches it but has not been
 * committed -- and reported both as "stale. Run `npm run generate:api` and commit the
 * result." An agent that had just run the generator read that, ran it again, and paid
 * another four and a half minutes for the identical failure (task-189).
 *
 * So the two questions are asked separately, and only one of them is the gate's:
 *
 *   1. *Does the client match the schema?* Snapshot the directory, regenerate it,
 *      compare. This is the invariant the repository actually needs, and it fails the
 *      gate.
 *   2. *Is it committed?* `git status`. This is **not** a failure -- see below -- but
 *      it is worth naming, because the house rule is `git add <explicit paths>` and
 *      generated output is exactly what that habit forgets.
 *
 * Why the second is not a failure: the gate runs *before* every commit, so a gate that
 * requires a commit to already exist can never be the last thing you run before making
 * one. The old check inverted its own rule, and the documented workaround -- commit the
 * generated files first, then run the gate -- means committing something no gate has
 * inspected. Asking whether the client matches the working tree's schema, rather than
 * HEAD's, is also strictly stronger: HEAD's may be arbitrarily old, and it aligns this
 * check with `check:api-schema`, which has always compared against the file on disk.
 *
 * An incomplete commit is still caught: any checkout that does not carry the
 * regenerated files -- a fresh clone, a colleague's, CI -- regenerates and finds the
 * mismatch at question 1.
 */

import { spawnSync } from "node:child_process";
import { readdirSync, readFileSync } from "node:fs";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

export const GENERATED = "src/api/generated";

/** Every file under `dir`, keyed by its path relative to `dir`. */
export function snapshot(dir) {
  const files = new Map();
  let entries;
  try {
    entries = readdirSync(dir, { recursive: true, withFileTypes: true });
  } catch (error) {
    if (error.code === "ENOENT") return files;
    throw error;
  }
  for (const entry of entries) {
    if (!entry.isFile()) continue;
    const absolute = path.join(entry.parentPath ?? entry.path, entry.name);
    files.set(path.relative(dir, absolute).split(path.sep).join("/"), readFileSync(absolute));
  }
  return files;
}

/** The paths on which two snapshots disagree, sorted so the output is stable. */
export function differences(before, after) {
  const changed = [];
  for (const [name, contents] of after) {
    const previous = before.get(name);
    if (previous === undefined || !previous.equals(contents)) changed.push(name);
  }
  for (const name of before.keys()) {
    if (!after.has(name)) changed.push(name);
  }
  return changed.sort();
}

/**
 * What to say when regenerating changed something.
 *
 * Deliberately does not tell the reader to run the generator: this check has just run
 * it, and the changed files are already on disk. Telling them to run it again is what
 * made the old message a dead end -- following it exactly reproduced the failure.
 */
export function staleMessage(changed) {
  return [
    `The generated API client did not match frontend/openapi.json.`,
    `Regenerating it changed:`,
    ...changed.map((name) => `  ${GENERATED}/${name}`),
    ``,
    `Those files have been regenerated in place. Review the diff and include`,
    `frontend/${GENERATED} in your commit.`,
  ].join("\n");
}

/**
 * What to say when the client is correct but uncommitted. Not a failure: see the
 * module comment. Names the paths because `git add` here takes explicit ones.
 */
export function uncommittedMessage(entries) {
  return [
    `Note: the generated API client matches frontend/openapi.json but is not committed:`,
    ...entries.map((entry) => `  ${entry}`),
    `The gate runs before the commit, so this does not fail it. Remember to include`,
    `frontend/${GENERATED} in the commit -- \`git add\` takes explicit paths here.`,
  ].join("\n");
}

function regenerate() {
  const runner = path.join("node_modules", "@hey-api", "openapi-ts", "bin", "run.js");
  const result = spawnSync(process.execPath, [runner], { stdio: "inherit" });
  if (result.error) {
    console.error(`Unable to run openapi-ts: ${result.error.message}`);
    process.exit(1);
  }
  if (result.status !== 0) process.exit(result.status ?? 1);
}

/** Tracked modifications and untracked additions under the generated directory. */
function gitStatus() {
  const result = spawnSync(
    "git",
    ["status", "--porcelain", "--untracked-files=all", "--", GENERATED],
    { encoding: "utf8" },
  );
  if (result.error || result.status !== 0) return [];
  return result.stdout.split("\n").filter((line) => line.trim());
}

function main() {
  const before = snapshot(GENERATED);
  regenerate();
  const changed = differences(before, snapshot(GENERATED));

  if (changed.length) {
    console.error(staleMessage(changed));
    process.exit(1);
  }

  const dirty = gitStatus();
  if (dirty.length) console.log(uncommittedMessage(dirty));
  console.log("Generated API client files match the OpenAPI document.");
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main();
}
