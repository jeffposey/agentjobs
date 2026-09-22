import { createHash } from "node:crypto";
import { existsSync } from "node:fs";
import { dirname, resolve } from "node:path";

/**
 * Which port each Playwright worker's server binds.
 *
 * Imported by `playwright.config.ts`, which starts one server per worker, and by
 * `e2e/fixtures.ts`, which hands each worker the URL of its own. Kept in its own module
 * so the two cannot disagree: the config resolves the block once, writes the first port
 * into `AGENTJOBS_E2E_PORT`, and every worker it forks reads the answer rather than
 * re-deriving it.
 */

export const PORT_ENV = "AGENTJOBS_E2E_PORT";
export const WORKERS_ENV = "AGENTJOBS_E2E_WORKERS";

const PORT_BASE = 20_000;
const PORT_SPAN = 10_000;

/**
 * The ceiling on workers, and it is the port arithmetic that sets it.
 *
 * The gate owns 20000-29999 and may not leave it: scripts/bench.py derives its own port
 * from 30000-39999, and `tests/test_gate_port_isolation.py` asserts the two ranges do
 * not meet, so a benchmark and a gate in one checkout can never want one socket. So the
 * span is divided rather than extended -- each checkout gets a block of `MAX_WORKERS`
 * consecutive ports, and there are `PORT_SPAN / MAX_WORKERS` blocks to hash into. Four
 * workers means 2500 blocks, so two checkouts collide with probability 1 in 2500 rather
 * than the 1 in 10000 a single port had, and the collision is the same loud bind failure
 * it has always been. Raising the ceiling spends that margin again.
 *
 * Four is also close to where the measurement stops paying. The suite parallelises by
 * file and its longest file is a floor no worker count can go under, so the useful range
 * ends around six; see docs/performance.md.
 */
export const MAX_WORKERS = 4;

/** How many blocks the span divides into. */
const BLOCKS = PORT_SPAN / MAX_WORKERS;

export const DEFAULT_WORKERS = 4;

/** Find the checkout this run belongs to, walking up from the working directory. */
export function checkoutRoot(): string {
  let directory = resolve(process.cwd());
  for (;;) {
    if (
      existsSync(resolve(directory, "pyproject.toml")) &&
      existsSync(resolve(directory, "frontend", "playwright.config.ts"))
    ) {
      return directory;
    }
    const parent = dirname(directory);
    if (parent === directory) {
      throw new Error(
        `Cannot find the AgentJobs checkout above ${process.cwd()}. ` +
          "Run Playwright from inside the checkout, or set " +
          `${PORT_ENV} to the first port this run should use.`,
      );
    }
    directory = parent;
  }
}

/** How many workers -- and so how many servers -- this run gets. */
export function workerCount(): number {
  const raw = process.env[WORKERS_ENV];
  if (raw === undefined || raw.trim() === "") {
    return DEFAULT_WORKERS;
  }
  const parsed = Number(raw);
  if (!Number.isInteger(parsed) || parsed < 1 || parsed > MAX_WORKERS) {
    throw new Error(
      `${WORKERS_ENV} must be a whole number between 1 and ${MAX_WORKERS}, got ${raw}. ` +
        "The ceiling is the port arithmetic in e2e/ports.ts, not a judgement about this machine.",
    );
  }
  return parsed;
}

/**
 * The first port this checkout owns, unless the environment names one explicitly.
 *
 * The port has to be unique per checkout. Several agents work this repository at once
 * in sibling worktrees, each required to run scripts/check.py before every commit, so
 * two gates running at the same time is the normal case rather than an unusual one. A
 * module-level constant made the second one fail to bind roughly four minutes into a
 * five-minute gate, naming a port rather than a cause.
 *
 * The block is derived from the checkout's own path, so it is stable for a given
 * worktree and a developer can work out which checkout owns which ports from the path
 * alone. Two checkouts whose paths hash into the same block would still collide; that
 * is a 1-in-2500 event which produces exactly today's loud failure, not a silent one.
 */
export function firstPort(): number {
  const override = process.env[PORT_ENV];
  if (override !== undefined && override.trim() !== "") {
    const parsed = Number(override);
    if (!Number.isInteger(parsed) || parsed < 1 || parsed > 65535) {
      throw new Error(`${PORT_ENV} must be a port number between 1 and 65535, got ${override}.`);
    }
    return parsed;
  }
  const digest = createHash("sha256").update(`${checkoutRoot()}\ne2e`).digest();
  return PORT_BASE + (digest.readUInt32BE(0) % BLOCKS) * MAX_WORKERS;
}

/** The port worker `slot` binds, given the first port of this run's block. */
export function portForSlot(first: number, slot: number): number {
  const port = first + slot;
  if (port > 65535) {
    throw new Error(
      `Worker ${slot} would bind port ${port}, which is not a port. ` +
        `${PORT_ENV} names the *first* of ${MAX_WORKERS} consecutive ports, ` +
        `so it has to be ${65535 - MAX_WORKERS + 1} or lower.`,
    );
  }
  return port;
}

/** Where worker `slot` finds its own server. */
export function serverUrlForSlot(slot: number): string {
  return `http://127.0.0.1:${portForSlot(firstPort(), slot)}`;
}
