import { describe, expect, it } from "vitest";

import { describeSkew } from "./skew";

const CONTRACT_A = "a".repeat(64);
const CONTRACT_B = "b".repeat(64);

describe("describeSkew", () => {
  it("says nothing when the bundle and the server were built from the same contract", () => {
    expect(
      describeSkew({
        bundleDigest: CONTRACT_A,
        serverDigest: CONTRACT_A,
        tabBundleId: "build-1",
        servedBundleId: "build-1",
      }),
    ).toBeNull();
  });

  it("reports the 2026-08-17 incident: same version and schema, different contract", () => {
    // The whole reason this is a digest rather than a version comparison. On the day,
    // the package version and the task-record schema version were identical on both
    // sides from start to finish, while `related` had been added to the task detail
    // response underneath a process that had been running since before it existed.
    // Anything that compares those two numbers reports agreement here.
    const skew = describeSkew({
      bundleDigest: CONTRACT_A,
      serverDigest: CONTRACT_B,
      tabBundleId: "build-1",
      servedBundleId: "build-1",
    });

    expect(skew?.kind).toBe("contract");
    expect(skew?.detail).toContain(CONTRACT_A.slice(0, 12));
    expect(skew?.detail).toContain(CONTRACT_B.slice(0, 12));
  });

  it("names both remedies, in the order to try them", () => {
    const skew = describeSkew({ bundleDigest: CONTRACT_A, serverDigest: CONTRACT_B });

    expect(skew?.remedies[0]).toContain("agentjobs restart");
    // frontend_dist/ is gitignored, so pulling and restarting is not enough on its own.
    expect(skew?.remedies[1]).toContain("npm run build");
  });

  it("says nothing when the server reports no digest at all", () => {
    // An older server, or one that answered before the field existed. A detector that
    // reads "cannot tell" as "out of step" puts a permanent banner on every install
    // that has not upgraded, which is how a warning stops being believed.
    expect(describeSkew({ bundleDigest: CONTRACT_A, serverDigest: null })).toBeNull();
    expect(describeSkew({ bundleDigest: CONTRACT_A })).toBeNull();
  });

  it("reports a rebuild this tab has not picked up, even with the contract unchanged", () => {
    // The common case by a wide margin: a frontend change touches no API route, so the
    // contract digest is identical and only the built assets moved.
    const skew = describeSkew({
      bundleDigest: CONTRACT_A,
      serverDigest: CONTRACT_A,
      tabBundleId: "build-1",
      servedBundleId: "build-2",
    });

    expect(skew?.kind).toBe("bundle");
    expect(skew?.detail).toContain("build-2");
  });

  it("offers the reload first when the tab is behind on both counts", () => {
    // Reloading is the cheap thing to try and may settle the contract mismatch too,
    // because the newer bundle on disk may be the one the server was built with.
    expect(
      describeSkew({
        bundleDigest: CONTRACT_A,
        serverDigest: CONTRACT_B,
        tabBundleId: "build-1",
        servedBundleId: "build-2",
      })?.kind,
    ).toBe("bundle");
  });

  it("says nothing about bundles when the server serves none it can name", () => {
    // A server with no build-info.json -- a bundle built before the field existed, or
    // a dev server with no bundle at all. Nothing is known, so nothing is claimed.
    expect(
      describeSkew({
        bundleDigest: CONTRACT_A,
        serverDigest: CONTRACT_A,
        tabBundleId: "build-1",
        servedBundleId: null,
      }),
    ).toBeNull();
  });

  it("gives each distinct mismatch its own fingerprint, so dismissing one shows the next", () => {
    const first = describeSkew({ bundleDigest: CONTRACT_A, serverDigest: CONTRACT_B });
    const second = describeSkew({ bundleDigest: CONTRACT_A, serverDigest: "c".repeat(64) });

    expect(first?.fingerprint).not.toBe(second?.fingerprint);
  });
});
