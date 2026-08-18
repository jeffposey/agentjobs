import { defineConfig } from "@playwright/test";

// Separate from playwright.config.ts because the benchmark supplies its own server.
// scripts/bench.py has already started AgentJobs over a throwaway project of a stated
// corpus size, and starting a second one here would measure a different corpus than
// the API and CLI sections just measured.
const baseURL = process.env.BENCH_BASE_URL;

if (!baseURL) {
  throw new Error("BENCH_BASE_URL is required; run this config through scripts/bench.py.");
}

export default defineConfig({
  testDir: "./e2e-bench",
  // The default testMatch only recognises *.spec.ts / *.test.ts, and these files are
  // deliberately named .bench.ts so nobody mistakes a timing run for a functional test.
  testMatch: "**/*.bench.ts",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: "line",
  timeout: 180_000,
  // The default 5s expect timeout is shorter than the thing being measured: a
  // list request currently takes about four seconds, so the benchmark would fail
  // to measure precisely the slowness it exists to record.
  expect: { timeout: 120_000 },
  use: {
    baseURL,
    browserName: "chromium",
    trace: "off",
  },
});
