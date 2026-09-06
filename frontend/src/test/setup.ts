import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterAll, afterEach, beforeAll } from "vitest";

import { apiMockServer } from "./api-mock";
import { DEFAULT_VIEWPORT, installMatchMedia, setViewport } from "./viewport";

beforeAll(() => {
  // jsdom has no matchMedia of its own; see `viewport.ts` for what this answers with.
  installMatchMedia();
  apiMockServer.listen({ onUnhandledRequest: "error" });
});
afterEach(() => {
  cleanup();
  setViewport(DEFAULT_VIEWPORT.width, DEFAULT_VIEWPORT.height);
  apiMockServer.resetHandlers();
});
afterAll(() => apiMockServer.close());
