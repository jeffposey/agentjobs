import { defineConfig } from "@hey-api/openapi-ts";

export default defineConfig({
  input: "openapi.json",
  output: {
    path: "src/api/generated",
  },
  plugins: ["@hey-api/typescript", "@hey-api/sdk", "@tanstack/react-query"],
});
