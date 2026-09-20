import { createHash } from "node:crypto";
import { existsSync } from "node:fs";
import { dirname, resolve } from "node:path";

import { defineConfig } from "@playwright/test";

// The port has to be unique per checkout. Several agents work this repository at once
// in sibling worktrees, each required to run scripts/check.py before every commit, so
// two gates running at the same time is the normal case rather than an unusual one. A
// module-level constant made the second one fail to bind roughly four minutes into a
// five-minute gate, naming a port rather than a cause.
//
// The port is derived from the checkout's own path, so it is stable for a given
// worktree and a developer can work out which checkout owns which port from the path
// alone. Two checkouts whose paths hash into the same slot would still collide; that
// is a 1-in-10000 event which produces exactly today's loud failure, not a silent one.
const PORT_ENV = "AGENTJOBS_E2E_PORT";
const PORT_BASE = 20000;
const PORT_SPAN = 10000;

/** Find the checkout this run belongs to, walking up from the working directory. */
function checkoutRoot(): string {
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
          `${PORT_ENV} to the port this run should use.`,
      );
    }
    directory = parent;
  }
}

/** The port this checkout owns, unless the environment names one explicitly. */
function checkoutPort(root: string): number {
  const override = process.env[PORT_ENV];
  if (override !== undefined && override.trim() !== "") {
    const parsed = Number(override);
    if (!Number.isInteger(parsed) || parsed < 1 || parsed > 65535) {
      throw new Error(`${PORT_ENV} must be a port number between 1 and 65535, got ${override}.`);
    }
    return parsed;
  }
  const digest = createHash("sha256").update(`${root}\ne2e`).digest();
  return PORT_BASE + (digest.readUInt32BE(0) % PORT_SPAN);
}

const root = checkoutRoot();
const port = checkoutPort(root);
const baseURL = `http://127.0.0.1:${port}`;

// Printed so a bind failure can be attributed without opening this file: the line says
// which checkout claimed the port and where the number came from.
console.log(`[e2e] checkout ${root} owns ${baseURL} (derived from the checkout path; ${PORT_ENV} overrides)`);

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  // No retries, deliberately. A blanket retry would also re-run a test whose browser the
  // application crashed. The gate retries one narrow case instead: a test whose browser was
  // already gone before it started, read from the JSON report below (task-404,
  // scripts/e2e_failures.py, which asserts this path).
  retries: 0,
  reporter: [["line"], ["json", { outputFile: "playwright-report/e2e-results.json" }]],
  use: {
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
  webServer: {
    command: "poetry run python e2e/run_server.py",
    url: `${baseURL}/health`,
    // Never attach to a server this run did not start. A gate that can silently
    // exercise another checkout's code is worse than one that fails to bind.
    reuseExistingServer: false,
    timeout: 30_000,
    // The server has no default of its own, so it cannot bind a port this config is
    // not watching -- the two halves cannot disagree about which port is in play.
    env: { [PORT_ENV]: String(port) },
  },
});
