import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterAll, afterEach, beforeAll } from "vitest";

import { apiMockServer } from "./api-mock";

beforeAll(() => apiMockServer.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  cleanup();
  apiMockServer.resetHandlers();
});
afterAll(() => apiMockServer.close());
