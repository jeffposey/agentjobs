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
/**
 * What `/api/model` answers by default: a machine with no model configured.
 *
 * A default handler for the same reason `/api/version` has one -- the drafting control
 * is on two forms, so a test about something else on either of them would otherwise
 * fail on an unhandled request. It answers `unconfigured` because that is the state of
 * every machine that has not opted in, so a test that says nothing about drafting
 * exercises the form exactly as it behaves today. Tests that are *about* drafting
 * override it with `apiMockServer.use(...)`.
 */
export const MODEL_UNCONFIGURED = {
  available: false,
  reason: "unconfigured",
  detail:
    "No model is configured on this machine, so AgentJobs cannot draft. " +
    "Add ~/.agentjobs/model.yaml or set ANTHROPIC_API_KEY for the server process.",
  model: null,
  calls_per_hour: null,
  calls_used: null,
};

/**
 * What `GET /api/projects/{id}/dispatch` answers by default: a machine with no
 * `dispatch.yaml` at all.
 *
 * A default handler for the reason `/api/model` has one, and more so. Since task-176 the
 * capture control reads this state to decide whether it may offer to start an agent on
 * what is being filed, and that control is in the header of every page -- so without a
 * default, a test about anything else anywhere in the app would fail on an unhandled
 * request. `configured: false` is the state of every machine that has not opted in, so a
 * test that says nothing about dispatch exercises the app exactly as that machine
 * behaves. Tests that are *about* dispatch override it with `apiMockServer.use`.
 */
export const DISPATCH_UNCONFIGURED = {
  project_id: "unknown",
  configured: false,
  master_enabled: false,
  project_enabled: false,
  can_dispatch: false,
  refusal: {
    reason: "not_configured",
    message: "No dispatch configuration exists on this machine.",
  },
  config_path: "~/.agentjobs/dispatch.yaml",
};

export const apiMockServer = setupServer(
  http.get("*/api/version", () => HttpResponse.json(VERSION_IN_STEP)),
  http.get("*/api/model", () => HttpResponse.json(MODEL_UNCONFIGURED)),
  http.get("*/api/projects/:projectId/dispatch", ({ params }) =>
    HttpResponse.json({ ...DISPATCH_UNCONFIGURED, project_id: String(params.projectId) }),
  ),
);
