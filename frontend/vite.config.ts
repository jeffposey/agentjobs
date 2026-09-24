import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { configDefaults, defineConfig } from "vitest/config";

export default defineConfig({
  base: "/app/",
  build: {
    emptyOutDir: true,
    outDir: "../src/agentjobs/frontend_dist",
  },
  plugins: [react(), tailwindcss()],
  test: {
    environment: "jsdom",
    exclude: [...configDefaults.exclude, "e2e/**"],
    setupFiles: "./src/test/setup.ts",
  },
  server: {
    port: 5173,
    // The status data file lives in the Python package, which reads it too (task-562).
    // The dev server refuses files outside the frontend unless they are allowed here.
    fs: { allow: [".", "../src/agentjobs/status_vocabulary.json"] },
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8765",
        changeOrigin: true,
      },
    },
  },
});
