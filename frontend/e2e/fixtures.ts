import { tmpdir } from "node:os";
import { resolve } from "node:path";

import { test as base, expect } from "@playwright/test";

import { serverUrlForSlot } from "./ports";

/**
 * Where the server on `port` keeps its own copy of the built frontend.
 *
 * The other half of this agreement is `private_bundle_dir` in `e2e/run_server.py`, and
 * the port is what the two derive it from -- the one thing they have always shared. A
 * spec that rewrites the bundle has to write to the copy its *own* worker is serving,
 * or it reloads three other browsers in the middle of whatever they were doing.
 */
function bundleDirForUrl(serverURL: string): string {
  const port = new URL(serverURL).port;
  return resolve(tmpdir(), `agentjobs-e2e-dist-${port}`);
}

/**
 * The test object every spec in this directory imports, instead of `@playwright/test`.
 *
 * It exists for one reason: **each worker gets its own server, its own project and its
 * own AgentJobs home**, so the suite can run several specs at once. Until task-369 there
 * was a single `webServer` and a single temporary project, and a dozen specs read
 * project-wide state to make their assertions -- `attention-badge.spec.ts` counts what
 * the whole project is blocking on and asserts the count went up by exactly one, and
 * `capture-tray.spec.ts` finds its own task by listing every task there is. Those are
 * correct assertions about a project that belongs to one test run, and unrunnable
 * beside a neighbour filing tasks into the same one.
 *
 * Isolating the *server* rather than the *project* is what keeps this to an import
 * swap. `run_server.py` already builds a throwaway project and a throwaway
 * `AGENTJOBS_HOME` per process, so a second server is a second everything -- including
 * the machine-level dispatch configuration that `dispatch.spec.ts` and
 * `live-runs.spec.ts` write, which is why neither of them has to be quarantined. The
 * alternative was one server serving a project per worker, which would have meant
 * threading a project id through roughly 170 hard-coded `_local` references and would
 * still have left those two specs sharing one machine.
 *
 * One thing a second server did not isolate by itself, and the first parallel run found
 * it: the built bundle on disk. `run_server.py` now copies that too, and `bundleDir`
 * below is how the one spec that rewrites it finds its own worker's copy.
 *
 * A worker's `parallelIndex` is its slot, and a slot outlives the worker process that
 * happens to be in it: Playwright replaces a crashed worker with one holding the same
 * index. So binding the server to the index rather than to the worker is what survives
 * a browser death.
 */
export const test = base.extend<
  Record<string, never>,
  { serverURL: string; bundleDir: string }
>({
  serverURL: [
    // The empty pattern is Playwright's required spelling for a fixture that depends on
    // no other fixture -- it reads the dependencies out of this parameter, and rejects
    // anything that is not a destructuring pattern at all. So the rule is switched off
    // here rather than worked around.
    // eslint-disable-next-line no-empty-pattern
    async ({}, use, workerInfo) => {
      await use(serverUrlForSlot(workerInfo.parallelIndex));
    },
    { scope: "worker" },
  ],

  bundleDir: [
    async ({ serverURL }, use) => {
      await use(bundleDirForUrl(serverURL));
    },
    { scope: "worker" },
  ],

  // Overriding the option, so `page.goto("/app/...")` and the `request` fixture both
  // reach this worker's server without a spec knowing there is more than one.
  baseURL: async ({ serverURL }, use) => {
    await use(serverURL);
  },
});

export { expect };
export type { APIRequestContext, Locator, Page, Request, Route } from "@playwright/test";
