import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { describe, expect, it } from "vitest";

import { BUNDLE_API_DIGEST } from "../api/apiDigest";
import { client } from "../api/generated/client.gen";
import { apiMockServer, VERSION_IN_STEP } from "../test/api-mock";
import { VersionSkew } from "./VersionSkew";

/**
 * The banner, driven by what `/api/version` actually answers.
 *
 * `src/api/skew.test.ts` owns the decision table; these own the wiring -- that a real
 * response reaches the decision, that a failed one reaches nothing, and that the tab
 * remembers the build it started on rather than the one it last saw.
 */

const OTHER_CONTRACT = "f".repeat(64);

function serves(payload: Record<string, unknown>) {
  apiMockServer.use(http.get("*/api/version", () => HttpResponse.json(payload)));
}

function renderSkew() {
  client.setConfig({ baseUrl: "http://localhost" });
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <VersionSkew />
    </QueryClientProvider>,
  );
  return queryClient;
}

describe("VersionSkew", () => {
  it("shows nothing when the server serves the contract this bundle was built from", async () => {
    const queryClient = renderSkew();
    await waitFor(() => expect(queryClient.isFetching()).toBe(0));

    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("names the mismatch and both remedies when the contracts differ", async () => {
    // Same package version, same schema version, different API shape: the 2026-08-17
    // incident, and the only comparison that catches it.
    serves({ ...VERSION_IN_STEP, api_digest: OTHER_CONTRACT });
    renderSkew();

    const banner = await screen.findByRole("alert");
    expect(banner).toHaveTextContent("disagree about the API");
    expect(banner).toHaveTextContent("agentjobs restart");
    expect(banner).toHaveTextContent("npm run build");
    expect(banner).toHaveTextContent(BUNDLE_API_DIGEST.slice(0, 12));
  });

  it("offers a reload rather than performing one", async () => {
    serves({ ...VERSION_IN_STEP, api_digest: OTHER_CONTRACT });
    renderSkew();

    // The tab may hold a half-written note. Detect and report; the human decides.
    expect(await screen.findByRole("button", { name: "Reload" })).toBeVisible();
  });

  it("stays dismissed once dismissed", async () => {
    serves({ ...VERSION_IN_STEP, api_digest: OTHER_CONTRACT });
    renderSkew();

    fireEvent.click(await screen.findByRole("button", { name: "Dismiss" }));

    await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
  });

  it("shows nothing when the server is unreachable", async () => {
    apiMockServer.use(http.get("*/api/version", () => HttpResponse.error()));
    const queryClient = renderSkew();
    await waitFor(() => expect(queryClient.isFetching()).toBe(0));

    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("shows nothing when the server is too old to report a digest", async () => {
    const { api_digest: _omitted, ...older } = VERSION_IN_STEP;
    serves(older);
    const queryClient = renderSkew();
    await waitFor(() => expect(queryClient.isFetching()).toBe(0));

    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("offers a reload when the server starts serving a build this tab is not running", async () => {
    serves({ ...VERSION_IN_STEP, bundle_id: "build-1" });
    const queryClient = renderSkew();
    await waitFor(() => expect(queryClient.isFetching()).toBe(0));
    expect(screen.queryByRole("status")).toBeNull();

    // A rebuild lands under the open tab. Nothing about the contract changed, which is
    // true of most rebuilds and is why the contract digest alone would miss this.
    serves({ ...VERSION_IN_STEP, bundle_id: "build-2" });
    await queryClient.refetchQueries();

    expect(await screen.findByRole("status")).toHaveTextContent("newer build");
  });
});
