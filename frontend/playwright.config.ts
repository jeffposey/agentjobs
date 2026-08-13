import { defineConfig } from "@playwright/test";

const port = 18940;
const baseURL = `http://127.0.0.1:${port}`;

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: "line",
  use: {
    baseURL,
    browserName: "chromium",
    trace: "retain-on-failure",
  },
  webServer: {
    command: "poetry run python e2e/run_server.py",
    url: `${baseURL}/health`,
    reuseExistingServer: false,
    timeout: 30_000,
  },
});
