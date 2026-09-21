import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterAll, afterEach, beforeAll, beforeEach } from "vitest";

import { setDraftStore } from "../report/draftStore";
import { apiMockServer } from "./api-mock";
import { DEFAULT_VIEWPORT, installMatchMedia, setViewport } from "./viewport";

beforeAll(() => {
  // jsdom has no matchMedia of its own; see `viewport.ts` for what this answers with.
  installMatchMedia();
  apiMockServer.listen({ onUnhandledRequest: "error" });
});
beforeEach(() => {
  // A fresh device per test. The capture draft store is a singleton for the lifetime of
  // a tab, which is right in a browser and wrong in a file of tests: a draft left by one
  // test is restored into the next one's form, and the next test is then exercising a
  // form somebody else filled in.
  setDraftStore(null);
});
afterEach(() => {
  cleanup();
  setViewport(DEFAULT_VIEWPORT.width, DEFAULT_VIEWPORT.height);
  apiMockServer.resetHandlers();
});
afterAll(() => apiMockServer.close());
