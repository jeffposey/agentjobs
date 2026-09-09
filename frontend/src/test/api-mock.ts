import { HttpResponse, http } from "msw";
import { setupServer } from "msw/node";

import { BUNDLE_API_DIGEST } from "../api/apiDigest";

/**
 * What `/api/version` answers when a test has not said otherwise: a server built from
 * exactly this bundle's contract, so the skew detector finds nothing.
 *
 * It is a default handler rather than one every test installs, because each render of
 * `App` polls that endpoint and `onUnhandledRequest: "error"` would otherwise turn a
 * component test about something else into a failure about version skew. Tests that
 * are *about* skew override it with `apiMockServer.use(...)`, and `resetHandlers()`
 * puts this one back.
 */
export const VERSION_IN_STEP = {
  version: "0.1.0",
  schema_version: 2,
  yaml_loader: "libyaml (yaml.CSafeLoader)",
  source_root: "/repo",
  source_commit: null,
  started_at: "2026-01-01T00:00:00Z",
  api_digest: BUNDLE_API_DIGEST,
  bundle_id: null,
  frontend_bundle: "present",
};

/**
 * Shared HTTP boundary for frontend tests. Tests add request handlers explicitly so
 * an unexpected API call fails instead of quietly reaching a real server.
 */
export const apiMockServer = setupServer(
  http.get("*/api/version", () => HttpResponse.json(VERSION_IN_STEP)),
);
