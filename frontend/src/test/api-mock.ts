import { setupServer } from "msw/node";

/**
 * Shared HTTP boundary for frontend tests. Tests add request handlers explicitly so
 * an unexpected API call fails instead of quietly reaching a real server.
 */
export const apiMockServer = setupServer();
