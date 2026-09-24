import { defineConfig } from "@playwright/test";

import { PORT_ENV, checkoutRoot, firstPort, portForSlot, workerCount } from "./e2e/ports";

// One server per worker. The port block is derived from the checkout's path -- see
// e2e/ports.ts for why, and for why four workers is the ceiling -- and resolved exactly
// once, here, in the process that reads this file. The first port is written back into
// the environment so every worker Playwright forks from here inherits the answer instead
// of re-deriving it from a working directory nobody promised would be the same.
/** Names the Playwright runner to each server it starts. See `webServer` below. */
const OWNER_ENV = "AGENTJOBS_E2E_OWNER_PID";

const workers = workerCount();
const first = firstPort();
process.env[PORT_ENV] = String(first);

const ports = Array.from({ length: workers }, (_, slot) => portForSlot(first, slot));
const baseURL = `http://127.0.0.1:${ports[0]}`;

// Printed so a bind failure can be attributed without opening this file: the line says
// which checkout claimed the block and where the numbers came from. Once, from the
// process that resolved them -- every worker loads this file too, and four copies of one
// line reads like four runs.
if (process.env.TEST_WORKER_INDEX === undefined) {
  console.log(
    `[e2e] checkout ${checkoutRoot()} owns ${ports.join(", ")} ` +
      `(derived from the checkout path; ${PORT_ENV} overrides the first)`,
  );
}

export default defineConfig({
  testDir: "./e2e",
  // Parallel by file, not within one. Several specs build their fixtures in a
  // `beforeAll` and read them across the tests that follow, and a few assert on an
  // ordering the tests above them established; `fullyParallel` would break both. The
  // file is also the unit the isolation audit in e2e/README.md classifies, so it is the
  // unit that can be reasoned about.
  fullyParallel: false,
  workers,
  // No retries, deliberately. A blanket retry would also re-run a test whose browser the
  // application crashed. The gate retries one narrow case instead: a test whose browser was
  // already gone before it started, read from the JSON report below (task-404,
  // scripts/e2e_failures.py, which asserts this path).
  retries: 0,
  reporter: [["line"], ["json", { outputFile: "playwright-report/e2e-results.json" }]],
  use: {
    // Worker 0's server, and a default rather than the operative value: every spec
    // imports `e2e/fixtures.ts`, which overrides this per worker. It is here so that a
    // spec written without the fixture still reaches a server rather than nothing.
    baseURL,
    browserName: "chromium",
    trace: "retain-on-failure",
    launchOptions: {
      // Chromium's on-device speech component, switched off because in a headless
      // Chromium it takes the renderer with it.
      //
      // Measured on Chromium 151.0.7922.34, the build Playwright bundles:
      // `SpeechRecognition.available({langs, processLocally: true})` crashes the
      // renderer outright -- "Target crashed", no exception, nothing a `try` can
      // catch. The same call in the same build **headed** answers `downloadable`,
      // and so does real Chrome 153 headless, so this is a headless-Chromium bug in
      // that component and not something a person filing a task can reach. The
      // dictation control asks that question once per page (task-172), which turned
      // twenty-three capture-form tests red and none of them for their own reason.
      //
      // With the feature off, `available` is simply absent, which is a state the
      // control already has to handle -- Safari and every pre-139 Chrome are in it --
      // and it takes the same path: the microphone is still rendered and still
      // usable, and the page says the audio goes to the browser's speech service.
      // So this hides no behaviour the suite was covering.
      args: ["--disable-features=OnDeviceWebSpeech,OnDeviceWebSpeechAvailable"],
    },
  },
  webServer: ports.map((port) => ({
    command: "poetry run python e2e/run_server.py",
    url: `http://127.0.0.1:${port}/health`,
    // Never attach to a server this run did not start. A gate that can silently
    // exercise another checkout's code is worse than one that fails to bind.
    reuseExistingServer: false,
    // Four interpreters importing the application at once on a machine that may be
    // running three gates. Thirty seconds was comfortable for one and is not for four.
    timeout: 90_000,
    // The server has no default of its own, so it cannot bind a port this config is
    // not watching -- the two halves cannot disagree about which port is in play.
    //
    // The owner is this process, the runner that starts every server and is meant to
    // stop them. A runner killed from outside -- a gate interrupted, a stage timed out --
    // never gets to, and on Windows its servers do not die with it: `poetry run` sits
    // between them and stays alive as long as its child does, so the server's own
    // parent is no signal. Measured 2026-09-24 (task-515): `taskkill /F` on the runner
    // left the server listening with its parent still running. `run_server.py` watches
    // this pid instead and stops when it goes.
    env: { [PORT_ENV]: String(port), [OWNER_ENV]: String(process.pid) },
  })),
});
